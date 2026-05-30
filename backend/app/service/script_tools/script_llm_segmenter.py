"""ScriptLens · 启发式切分全失败时的 LLM 兜底切场。

定位
====

仅在 ``script_segmenter.segment_script`` 返回 ``fallback_strategy='single_scene'``
（即完全无集号 / 无数字场号 / 无裸场景头）且文本量 ≥ 5000 字时启用。
属于行业混合 pipeline 的"残段语义分类"环节：

  规则识别 sluglines/transitions          → script_segmenter.py（主路径，零成本）
  ↓ 全失败
  LLM 给残余段做情节单元切分              → 本模块（兜底，按段调一次）
  ↓ 失败
  整篇作为单场（零丢失）                  → script_segmenter._fallback_single_scene

参考：ETH 2025 "Ranking Movies with LLMs" §3.2 hybrid screenplay segmentation。
不取代规则切分；仅在规则路径已无信号时临时调一次便宜的 LLM。

非目标
======

- 不识别集号 / 场号编号（这些已在规则路径处理）
- 不做剧本格式标准化（不输出 fountain / fdx 格式）
- 不做 ≥ 5 万字超长剧本处理（直接降级到 single_scene，避免单次 token 失控）
- 不做并发切分（单次 LLM 即可，复杂度换稳定性）

输出契约
========

成功 → ``List[ParsedScene]``，每个 scene：
  - episode_no = None（无集号信息）
  - scene_no = "L%d"（L 表示 LLM-segmented，与规则路径的 "1-1" / "v0" 区分）
  - characters / scene_label 来自 LLM
  - text 由原段落拼接而成（零丢失：所有段落都进入某场）

失败（任一）→ None（上层保留 single_scene）：
  - LLM 调用异常
  - LLM 输出非 JSON 对象 / 缺 scenes 字段
  - 切分结果只 1 场（等于没切，浪费 LLM 调用还不如保留 single_scene）
  - 切分结果有重叠 / 漏段（结构错乱）
"""

from __future__ import annotations

import logging
from typing import List, Optional

from service.core.ingestion.script_segmenter import ParsedScene
from service.script_tools.llm_caller import (
    LlmCaller,
    ModelTier,
    ScoreLLMError,
    TokenBudget,
)

logger = logging.getLogger(__name__)


# 触发 / 拒绝阈值（命名常量，依据来自实际剧本采样）：
#
# - LLM_SEGMENT_MIN_CHARS = 5_000：低于这个字数的剧本即使全是 single_scene 也不
#   值得调 LLM——实测一两千字的短篇直接整篇当一场，UI 体验也能接受。
# - LLM_SEGMENT_MAX_CHARS = 80_000：上限。qwen-max-latest 输入 256k token、
#   中文 ≈ 1.5 字符 / token，理论可吃 ~170k 字；但短剧通常 6 万字以内，超过
#   8 万字基本是分章节的长剧本，应该走更复杂的分段策略，单次 LLM 失控风险高。
# - LLM_SEGMENT_TARGET_SCENES = 30：期望产出 5-30 场；少于 5 场说明 LLM 没
#   切动，多于 30 场说明切碎了。

LLM_SEGMENT_MIN_CHARS = 5_000
LLM_SEGMENT_MAX_CHARS = 80_000
LLM_SEGMENT_MIN_SCENES = 2
LLM_SEGMENT_MAX_SCENES = 30


_SYSTEM_PROMPT = """你是中文剧本结构识别助手。给定一段没有「集」「场」编号标记的剧本正文，你要按情节单元（场景）切分。

什么算一个"场景"：
- 同一时空（地点 + 时间不变）
- 围绕同一组人的同一段动作 / 同一场对话
- 时空切换（地点变 / 时间跳跃 / 进入回忆）就是新场的开始

输出契约（必须严格遵守）：
- 只输出**一个 JSON 对象**：{"scenes": [...]}
- 每个 scene = {"start_para": int, "end_para": int, "scene_label": str, "characters": [str]}
- start_para / end_para 是段落 index（含端点），从 0 计数；用户消息里每段以 [N] 开头标了 index
- 切分必须**完整覆盖**所有段落，不重叠、不漏段
- scene_label ≤ 20 字，描述这场戏的核心（如「客厅 日内 争吵」「车上回忆」）
- characters 列出本场出场人物（去重，≤ 6 人，无明确人物就空数组）
- 切分粒度：单场建议 200-2000 字；不要切太碎（每段一场）也不要太大（半本剧一场）
- 如果整段剧本结构非常模糊、无法可靠切分，输出 {"scenes": []}（上层会回退到整篇单场）
"""


def _build_user_prompt(body_paragraphs: List[str]) -> str:
    """把段落标 index，以「[N] 段内容」形式送给 LLM。

    LLM 用 start_para / end_para 引用 index，不会有"句号边界"歧义。
    """
    lines: List[str] = []
    for i, para in enumerate(body_paragraphs):
        text = para.strip()
        if not text:
            continue
        lines.append(f"[{i}] {text}")
    return (
        f"剧本正文（共 {len(body_paragraphs)} 段）：\n\n"
        + "\n".join(lines)
        + "\n\n请按情节单元切分，输出 JSON。"
    )


async def llm_resegment(
    body_paragraphs: List[str],
    *,
    body_start_in_full: int,
    caller: LlmCaller,
) -> Optional[List[ParsedScene]]:
    """对 ``body_paragraphs`` 做 LLM 切场。

    Args:
        body_paragraphs: 待切分的段落列表（已剔除 metadata 头部）。
        body_start_in_full: ``body_paragraphs[0]`` 在原始 paragraphs 中的 index，
            用于回填 ParsedScene.start_idx / end_idx，让上层 ``ScriptDbWriter``
            的"段落级回链"保持一致。
        caller: LLM caller。

    Returns:
        成功 → ParsedScene 列表（≥2 场，零丢失）；失败 → None。
    """
    if not body_paragraphs:
        return None

    total_chars = sum(len(p) for p in body_paragraphs)
    if total_chars < LLM_SEGMENT_MIN_CHARS:
        logger.info(
            "llm_resegment skip: chars=%d below min=%d",
            total_chars, LLM_SEGMENT_MIN_CHARS,
        )
        return None
    if total_chars > LLM_SEGMENT_MAX_CHARS:
        logger.warning(
            "llm_resegment skip: chars=%d above max=%d (degrade to single_scene)",
            total_chars, LLM_SEGMENT_MAX_CHARS,
        )
        return None

    prompt = _build_user_prompt(body_paragraphs)

    try:
        resp = await caller.call_json(
            prompt=prompt,
            tier=ModelTier.MINI,  # 切分是结构活，便宜模型够用
            system_message=_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=TokenBudget.LLM_RESEGMENT,
        )
    except ScoreLLMError as exc:
        logger.warning("llm_resegment LLM failed: %s", exc)
        return None

    parsed = resp.parsed if isinstance(resp.parsed, dict) else None
    if not parsed:
        logger.warning("llm_resegment: LLM returned non-object")
        return None

    raw_scenes = parsed.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        logger.info("llm_resegment: empty scenes (LLM declined to segment)")
        return None

    return _coerce_to_parsed_scenes(
        raw_scenes,
        body_paragraphs=body_paragraphs,
        body_start_in_full=body_start_in_full,
    )


def _coerce_to_parsed_scenes(
    raw_scenes: List[dict],
    *,
    body_paragraphs: List[str],
    body_start_in_full: int,
) -> Optional[List[ParsedScene]]:
    """把 LLM 输出转成 ParsedScene 列表。

    严格性优先于宽容性：任何"重叠 / 漏段 / 端点越界"都直接整体丢弃，
    回退到 single_scene。原因：错位的切分边界会传染下游所有 LLM 调用
    （评分 / 关系图 / 高光），错切比不切更糟。
    """
    n = len(body_paragraphs)
    out: List[ParsedScene] = []
    seen_paras: set = set()

    for idx, item in enumerate(raw_scenes):
        if not isinstance(item, dict):
            continue
        try:
            sp = int(item.get("start_para", -1))
            ep = int(item.get("end_para", -1))
        except (TypeError, ValueError):
            continue
        if sp < 0 or ep < sp or ep >= n:
            logger.warning(
                "llm_resegment: invalid range [%d, %d] (n=%d)", sp, ep, n,
            )
            return None
        if any(p in seen_paras for p in range(sp, ep + 1)):
            logger.warning("llm_resegment: overlap at scene #%d range=[%d,%d]", idx, sp, ep)
            return None
        seen_paras.update(range(sp, ep + 1))

        scene_label = str(item.get("scene_label") or f"未命名场景 {idx + 1}").strip()[:30]
        chars_raw = item.get("characters")
        characters: List[str] = []
        if isinstance(chars_raw, list):
            for c in chars_raw:
                name = str(c or "").strip()
                if name and name not in characters:
                    characters.append(name)
                if len(characters) >= 6:
                    break

        text = "\n".join(p for p in body_paragraphs[sp : ep + 1] if p.strip())
        if not text.strip():
            continue

        out.append(
            ParsedScene(
                episode_no=None,
                scene_no=f"L{idx + 1}",
                scene_label=scene_label,
                characters=characters,
                text=text,
                start_idx=body_start_in_full + sp,
                end_idx=body_start_in_full + ep,
            )
        )

    if len(out) < LLM_SEGMENT_MIN_SCENES:
        logger.info(
            "llm_resegment: produced only %d scene(s), reject (use single_scene)",
            len(out),
        )
        return None
    if len(out) > LLM_SEGMENT_MAX_SCENES:
        logger.warning(
            "llm_resegment: produced %d scenes (over max %d), trimming to top %d",
            len(out), LLM_SEGMENT_MAX_SCENES, LLM_SEGMENT_MAX_SCENES,
        )
        out = out[:LLM_SEGMENT_MAX_SCENES]

    # 检查零丢失：所有 body 段落（非空段）都进入了某场
    body_non_empty = {i for i, p in enumerate(body_paragraphs) if p.strip()}
    missing = body_non_empty - seen_paras
    if missing:
        logger.warning(
            "llm_resegment: %d paragraphs not covered (would lose data); reject",
            len(missing),
        )
        return None

    return out
