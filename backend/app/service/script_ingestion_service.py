"""ScriptLens 剧本摄取服务（顶层编排）。

链路：本地文件 → load_script_paragraphs → segment_script → ScriptDbWriter.

调用方：
- `router/script_rt.py POST /api/scripts/upload` （HTTP 入口；走两阶段异步）
- `eval/_e2e_dryrun.py` （离线 e2e 验证；走单阶段 ingest）

不变式：
- 单个剧本的 scenes 在一个事务里写完，要么全成要么全无
- 任何步骤异常都向上抛，不静默吞错（fail-fast）
- 解析/切分耗时大头时，可以传 `progress_cb` 让上游推 SSE 进度

两种调用模式：
1. **同步单阶段** —— `ingest()`：一次性 INSERT 两表 + status='ready'，dryrun / CLI 用
2. **异步两阶段** —— `start_pending()` + `run_ingestion()`：HTTP upload 立即返回
   `script_id` + status='pending'，BackgroundTask 跑完 INSERT scenes 并
   UPDATE status='ready'；任何阶段异常 → mark_failed，前端可见 failure_reason

embedding 历史：v0 曾每场写一份 1024 维向量到 `script_chunks` 表用于 RAG，
v1 起彻底拆除——理由见 `docs/04-script-pipeline.md` §6（评分/证据/任务派发
三条核心链路均不查向量，唯一调用方 locate_scenes_tool 用 BM25 已足够）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from service.core.ingestion.script_loader import (
    EmptyScriptError,
    ScannedPdfError,
    load_script_paragraphs,
)
from service.core.ingestion.script_pgvector_writer import (
    ScriptPgVectorWriter,
    WrittenScene,
)
from service.core.ingestion.script_segmenter import (
    SegmentResult,
    segment_script,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    script_id: str
    title: str
    source_format: str
    total_episodes: int
    total_scenes: int
    total_chars: int
    fallback_strategy: Optional[str]
    parsing_warnings: List[str]
    written_scenes: List[WrittenScene]


class ScriptIngestionService:
    """两步走：load+segment → persist。"""

    def __init__(
        self,
        *,
        writer: Optional[ScriptPgVectorWriter] = None,
    ) -> None:
        self.writer = writer or ScriptPgVectorWriter()

    # ============================================================
    # 同步单阶段（dryrun / CLI 离线用）
    # ============================================================

    def ingest(
        self,
        *,
        file_path: Path,
        user_id: int,
        title: Optional[str] = None,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> IngestResult:
        """同步主入口；任一阶段失败立即抛错。两表一次性 INSERT + status='ready'。"""
        t0 = time.perf_counter()
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))

        suffix = file_path.suffix.lower().lstrip(".")
        title = title or file_path.stem

        paragraphs, seg = self._load_segment(
            file_path=file_path,
            progress_cb=progress_cb,
        )

        if progress_cb:
            progress_cb("persisting", {"scenes": seg.total_scenes})
        script_id, written = self.writer.insert_script_with_scenes(
            user_id=user_id,
            title=title,
            source_format=suffix,
            raw_storage_path=str(file_path),
            total_episodes=seg.total_episodes,
            scenes=seg.scenes,
        )

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("ingest.done script_id=%s elapsed_ms=%s", script_id, elapsed_ms)
        if progress_cb:
            progress_cb("done", {"script_id": script_id, "elapsed_ms": elapsed_ms})

        return IngestResult(
            script_id=script_id,
            title=title,
            source_format=suffix,
            total_episodes=seg.total_episodes,
            total_scenes=seg.total_scenes,
            total_chars=seg.total_chars,
            fallback_strategy=seg.fallback_strategy,
            parsing_warnings=seg.parsing_warnings,
            written_scenes=written,
        )

    # ============================================================
    # 异步两阶段（HTTP upload 用）
    # ============================================================

    def start_pending(
        self,
        *,
        file_path: Path,
        user_id: int,
        title: Optional[str] = None,
    ) -> str:
        """阶段一：file 已落盘 → INSERT scripts(status='pending') → 返回 script_id。

        立即返回，不做解析。后续由 `run_ingestion(script_id, file_path)`
        在 BackgroundTask 里跑完整链路。
        """
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        suffix = file_path.suffix.lower().lstrip(".")
        title = title or file_path.stem
        return self.writer.create_pending_script(
            user_id=user_id,
            title=title,
            source_format=suffix,
            raw_storage_path=str(file_path),
        )

    def run_ingestion(
        self,
        *,
        script_id: str,
        file_path: Path,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> IngestResult:
        """阶段二：跑完整链路并 UPDATE status='ready'；失败时 mark_failed 后抛错。

        BackgroundTask 入口。失败 reason 会落到 scripts.failure_reason，前端能看到。
        """
        t0 = time.perf_counter()
        try:
            self.writer.update_status(script_id, "parsing")
            paragraphs, seg = self._load_segment(
                file_path=file_path,
                progress_cb=progress_cb,
            )

            self.writer.update_status(script_id, "indexing")
            if progress_cb:
                progress_cb("persisting", {"scenes": seg.total_scenes})
            written = self.writer.complete_script_with_scenes(
                script_id=script_id,
                total_episodes=seg.total_episodes,
                scenes=seg.scenes,
            )
        except Exception as e:
            # 把"用户能看懂的错"（扫描件 / 空文档 / 未知格式）原样落 failure_reason；
            # 其它内部异常加 type 前缀，便于运维查日志，但不再泄露 uuid 文件名。
            if isinstance(e, (ScannedPdfError, EmptyScriptError)):
                reason = str(e)
            else:
                reason = f"{type(e).__name__}: {e}"
            logger.exception("run_ingestion failed script_id=%s reason=%s", script_id, reason)
            try:
                self.writer.mark_failed(script_id, reason)
            except Exception:
                logger.exception("mark_failed also failed script_id=%s", script_id)
            raise

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("run_ingestion.done script_id=%s elapsed_ms=%s", script_id, elapsed_ms)
        if progress_cb:
            progress_cb("done", {"script_id": script_id, "elapsed_ms": elapsed_ms})

        return IngestResult(
            script_id=script_id,
            title=file_path.stem,
            source_format=file_path.suffix.lower().lstrip("."),
            total_episodes=seg.total_episodes,
            total_scenes=seg.total_scenes,
            total_chars=seg.total_chars,
            fallback_strategy=seg.fallback_strategy,
            parsing_warnings=seg.parsing_warnings,
            written_scenes=written,
        )

    # ============================================================
    # 内部：load + segment（不写库）
    # ============================================================

    def _load_segment(
        self,
        *,
        file_path: Path,
        progress_cb: Optional[Callable[[str, dict], None]] = None,
    ) -> tuple[List[str], SegmentResult]:
        if progress_cb:
            progress_cb("loading", {"file": str(file_path)})
        paragraphs = load_script_paragraphs(file_path)
        if not paragraphs:
            # PDF 已在 _load_pdf 内细化为 ScannedPdfError / EmptyScriptError；
            # 这里覆盖 docx / txt / md 的空文档分支，文案对用户友好，且不再泄露 uuid 文件名。
            raise EmptyScriptError(
                "上传的剧本解析后没有任何文字内容（可能是空文档或文件损坏），请确认文件内容后重新上传。"
            )
        logger.info("ingest.loaded paragraphs=%s file=%s", len(paragraphs), file_path.name)

        if progress_cb:
            progress_cb("segmenting", {"paragraphs": len(paragraphs)})
        seg: SegmentResult = segment_script(paragraphs)
        if not seg.scenes:
            # 段落非空但切不出场景 —— 通常是格式严重不符（缺集号/场号头）。
            # 不暴露 uuid，给可操作建议。
            raise EmptyScriptError(
                "已读取剧本文字，但无法切分出有效场景；"
                "请确认剧本是否包含集号 / 场号标记（如「第1集」「1-1」「场1」等）后再重新上传。"
            )
        logger.info(
            "ingest.segmented scenes=%s eps=%s fallback=%s file=%s",
            seg.total_scenes,
            seg.total_episodes,
            seg.fallback_strategy,
            file_path.name,
        )

        # 启发式规则全失败 → LLM 兜底（行业混合 pipeline 的"残段语义分类"）
        if seg.fallback_strategy == "single_scene" and seg.total_chars >= 5_000:
            if progress_cb:
                progress_cb("llm_segmenting", {"chars": seg.total_chars})
            llm_seg = self._maybe_llm_resegment(paragraphs, seg)
            if llm_seg is not None:
                seg = llm_seg
                logger.info(
                    "ingest.llm_segmented scenes=%s file=%s (heuristic single_scene → LLM fallback)",
                    seg.total_scenes, file_path.name,
                )

        return paragraphs, seg

    def _maybe_llm_resegment(
        self,
        paragraphs: List[str],
        seg: SegmentResult,
    ) -> Optional[SegmentResult]:
        """规则切分全失败时的 LLM 兜底切场。

        失败返回 None，上层保留原 single_scene 结果（零丢失）。本方法在
        ``run_in_executor`` 启动的工作线程里调用，线程内无现存事件循环，
        ``asyncio.run`` 安全。
        """
        try:
            import asyncio  # noqa: WPS433（局部导入：避免顶层引入 asyncio 给同步路径加噪）

            from service.script_tools.llm_caller import LlmCaller
            from service.script_tools.script_llm_segmenter import llm_resegment
        except ImportError as exc:
            logger.warning("ingest._maybe_llm_resegment: import failed err=%s", exc)
            return None

        # body_start：seg 已经走过 metadata_block 抽取；single_scene 路径下
        # body_start = len(metadata_paragraphs) 即可还原。这里复用 segmenter
        # 的同款逻辑：metadata_block 是非空段落以 \n 拼接。
        if seg.metadata_block:
            metadata_lines = seg.metadata_block.split("\n")
            body_start = 0
            metadata_seen = 0
            for i, p in enumerate(paragraphs):
                if not p.strip():
                    continue
                if metadata_seen >= len(metadata_lines):
                    body_start = i
                    break
                metadata_seen += 1
        else:
            body_start = 0

        body = paragraphs[body_start:]
        if not body:
            return None

        try:
            scenes = asyncio.run(
                llm_resegment(
                    body,
                    body_start_in_full=body_start,
                    caller=LlmCaller(),
                )
            )
        except Exception as exc:
            logger.warning("ingest._maybe_llm_resegment: asyncio.run raised err=%s", exc)
            return None

        if not scenes:
            return None

        new_warnings = list(seg.parsing_warnings) + [
            f"启发式规则全失败，已用 LLM 兜底切出 {len(scenes)} 场（结构可能不完美，建议核对）",
        ]
        total_chars = sum(len(s.text) for s in scenes)
        return SegmentResult(
            metadata_block=seg.metadata_block,
            scenes=scenes,
            total_episodes=0,
            total_scenes=len(scenes),
            total_chars=total_chars,
            parsing_warnings=new_warnings,
            fallback_strategy="llm_resegmented",
        )
