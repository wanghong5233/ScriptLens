"""ScriptLens · 评分流水线进度追踪器（进程内 in-memory）。

设计取舍
========
- take-home 单实例 backend，不需要 Redis / DB 持久化进度。
- BackgroundTask 跑在同一个进程；前端轮询 GET /scripts/{id}/progress
  直接读这个模块级单例就够了。
- 进程重启 → 进度丢失。但此时前端会发现 reports 表为空 + tracker 也无快照，
  会回退到「正在生成中…」朴素 spin，行为等价于改造前的体验，无回归风险。

线程安全
========
- FastAPI 把 sync 函数放线程池跑、async 函数放主 event loop 跑；多个并发
  reanalyze 可能同时读写 _store，所以 dict 的写入用互斥锁保护。

GC
==
- 每次 start() 时清理 5 分钟内未更新的快照，避免长跑导致 OOM
  （正常一次 generate_report 5~120 秒就完成了）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 7 个流水线阶段，与 service.script_report_service.generate_report 一一对应。
# (id, label, description) —— description 用于前端 tooltip / 下方解说，
# 让用户明白每一步在做什么，而不是单纯看见一堆 spinner。
_REPORT_PIPELINE_STAGES: List[tuple[str, str, str]] = [
    (
        "loading_meta",
        "加载剧本元数据",
        "读取剧本基本信息（集数 / 场数 / 字数），用于后续评分时给 LLM 上下文",
    ),
    (
        "running_tag_pipeline",
        "运行标签流水线",
        "检查并补齐 plot_unit_tags / character_entities / relationship / drama_tags，缺失时触发 bundle_extractor",
    ),
    (
        "computing_signals",
        "计算派生信号",
        "按 rubric 的 rule + llm bundles 统一计算信号集合，作为六维聚合输入",
    ),
    (
        "scoring_dimensions",
        "六维聚合与分档",
        "dimension_aggregator 聚合后做 genre_weights 与 percentile tier，得到 tier/confidence/coverage",
    ),
    (
        "aggregating_decision",
        "汇总整体决策",
        "合规评分接入决策矩阵，输出最终 decision 与 decision_inputs",
    ),
    (
        "building_pacing_and_actions",
        "生成节奏曲线与改写动作",
        "按 plot_unit 强度序列构建 pacing_curve，并基于弱信号模板生成 improvement_actions",
    ),
    (
        "extracting_narrative",
        "抽取速览 / 节拍 / 人物图",
        "并行抽取速览卡 logline、三幕节拍 beat_sheet、人物关系图 character_graph、看点 reward_events，前端 5 个 tab 的叙事层数据",
    ),
    (
        "persisting",
        "写入数据库",
        "事务写入 scoring_runs / script_scores / scoring_improvement_actions / reports.decision_payload",
    ),
]


@dataclass
class StageInfo:
    id: str
    label: str
    description: str
    state: str = "pending"  # pending | running | done | failed
    detail: Optional[str] = None  # 当前阶段的动态文案（"识别到 14 个爽点事件"）
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass
class ProgressSnapshot:
    script_id: str
    started_at: float
    updated_at: float
    final: bool = False
    error: Optional[str] = None
    stages: List[StageInfo] = field(default_factory=list)
    current_index: int = 0


class _ProgressTracker:
    def __init__(self) -> None:
        self._store: Dict[str, ProgressSnapshot] = {}
        self._lock = threading.Lock()
        self._stale_seconds = 300  # 5 分钟未更新视为过期，触发 GC

    def start(self, script_id: str) -> None:
        """流水线起步时调一次：清空旧快照，写入阶段 pending 列表。"""
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            self._store[script_id] = ProgressSnapshot(
                script_id=script_id,
                started_at=now,
                updated_at=now,
                stages=[
                    StageInfo(id=sid, label=lbl, description=desc)
                    for (sid, lbl, desc) in _REPORT_PIPELINE_STAGES
                ],
            )

    def update_stage(
        self,
        script_id: str,
        stage_id: str,
        state: str,
        detail: Optional[str] = None,
    ) -> None:
        """切换某阶段状态。state ∈ {running, done, failed}。"""
        if state not in ("running", "done", "failed"):
            raise ValueError(f"unknown state={state!r}")
        now = time.time()
        with self._lock:
            snap = self._store.get(script_id)
            if snap is None:
                return
            snap.updated_at = now
            for idx, st in enumerate(snap.stages):
                if st.id != stage_id:
                    continue
                if state == "running":
                    st.state = "running"
                    st.detail = detail
                    if st.started_at is None:
                        st.started_at = now
                    snap.current_index = idx
                elif state == "done":
                    st.state = "done"
                    if detail is not None:
                        st.detail = detail
                    st.completed_at = now
                    # 推进 current_index 到下一阶段（最后一阶段时停留）
                    snap.current_index = min(idx + 1, len(snap.stages) - 1)
                else:  # failed
                    st.state = "failed"
                    st.detail = detail
                    st.completed_at = now
                    snap.current_index = idx
                return

    def update_detail(self, script_id: str, detail: str) -> None:
        """只更新「当前阶段」的 detail（不切状态）。

        用途：并行评分跑了一半，想把 "已完成 3/6 维（开场钩子、动机、风险）"
        实时回写给前端。
        """
        now = time.time()
        with self._lock:
            snap = self._store.get(script_id)
            if snap is None:
                return
            snap.updated_at = now
            if 0 <= snap.current_index < len(snap.stages):
                snap.stages[snap.current_index].detail = detail

    def finalize(self, script_id: str, error: Optional[str] = None) -> None:
        """流水线收尾时调一次：标记 final=True；失败时携带 error。"""
        now = time.time()
        with self._lock:
            snap = self._store.get(script_id)
            if snap is None:
                return
            snap.final = True
            snap.error = error
            snap.updated_at = now
            if error is not None and 0 <= snap.current_index < len(snap.stages):
                # 让"当前阶段"显式变 failed，前端时间轴上能看到红点
                cur = snap.stages[snap.current_index]
                if cur.state == "running":
                    cur.state = "failed"
                    cur.detail = error
                    cur.completed_at = now

    def get(self, script_id: str) -> Optional[ProgressSnapshot]:
        with self._lock:
            return self._store.get(script_id)

    def to_dict(self, script_id: str) -> Optional[Dict[str, Any]]:
        snap = self.get(script_id)
        if snap is None:
            return None
        # asdict 会递归转 dataclass，包括 List[StageInfo]
        return asdict(snap)

    def _gc_locked(self, now: float) -> None:
        cutoff = now - self._stale_seconds
        stale = [sid for sid, snap in self._store.items() if snap.updated_at < cutoff]
        for sid in stale:
            self._store.pop(sid, None)
        if stale:
            logger.debug("progress_tracker GC dropped %s stale entries", len(stale))


# 模块级单例。所有调用方（generate_report / router）都共享。
tracker = _ProgressTracker()
