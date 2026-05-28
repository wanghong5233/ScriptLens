"""场景库 helper（DB 只读层）。

把"按 script_id 取场景"的所有变体 SQL 集中到这里，让上层评分工具
（风险扫描与评分信号链路共用）
不直接写 SQL，只调本模块函数。

不变式：
- 所有查询 limit 默认有界（防止全表 5000 场全部加载到内存）
- 返回 dataclass 而非 dict，让评分工具的类型签名清晰
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from utils.database import engine as default_engine

logger = logging.getLogger(__name__)


# ============================================================
# Evidence quote 长度上限
# ============================================================
#
# 取值依据（不是魔法数字）：
# - 前端 evidence chip 单行容器宽度 ≈ 720px（rail segment 宽 750px - 内边距）
# - 默认字号 14px，中文字宽 ≈ 14px → 单行容纳约 51 个中文字符
# - 多行展示限 2 行 → 102 字理论上限，留 10% 安全边 → 90 字
# - LLM 输出预留省略号 + JSON 结构 overhead → reward / risk evidence 上限取 80 字
#   （比展示上限 90 字小，留缓冲；超 80 由前端 ellipsis 处理）
#
# 调用站：
# - extract_quote(max_chars=EVIDENCE_QUOTE_MAX_LEN)
# - 风险/评分提示模板「evidence ≤ 80 字」
# - risk_screener evidence excerpt
# - 前端 MustReadChip / HighlightRow scene_summary 截断
#
# 任何调用方需要不同长度上限时**必须在此声明常量**，不允许 inline 数字。

EVIDENCE_QUOTE_MAX_LEN: int = 90
LLM_EVIDENCE_MAX_LEN: int = 80
SCENE_SUMMARY_MAX_LEN: int = 70


@dataclass
class Scene:
    """场景内存表示。与 DB 字段 1:1。"""

    id: str
    script_id: str
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: List[str]
    start_line: Optional[int]
    end_line: Optional[int]
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text or "")


def get_scene(*, scene_id: str, engine: Engine = default_engine) -> Optional[Scene]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE id = :sid
                """
            ),
            {"sid": scene_id},
        ).mappings().first()
    if not row:
        return None
    return Scene(**dict(row))


def get_all_scenes(
    *,
    script_id: str,
    limit: int = 5000,
    engine: Engine = default_engine,
) -> List[Scene]:
    """全剧所有场景，按 episode_no/scene_no/start_line 排序。"""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                ORDER BY episode_no NULLS LAST, scene_no, start_line
                LIMIT :limit
                """
            ),
            {"sid": script_id, "limit": limit},
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def get_first_episode_scenes(
    *,
    script_id: str,
    n_episodes: int = 3,
    engine: Engine = default_engine,
) -> List[Scene]:
    """取前 N 集的所有场景（用于 opening_hook 评分）。

    若剧本无 episode_no（fallback 切分），则按场号顺序取前 6 场作为「开场窗口」。
    """
    all_scenes = get_all_scenes(script_id=script_id, engine=engine)
    if not all_scenes:
        return []
    if all(s.episode_no is None for s in all_scenes):
        return all_scenes[:6]
    eps_seen: set[int] = set()
    out: List[Scene] = []
    for s in all_scenes:
        if s.episode_no is None:
            continue
        if s.episode_no not in eps_seen:
            if len(eps_seen) >= n_episodes:
                break
            eps_seen.add(s.episode_no)
        if s.episode_no in eps_seen:
            out.append(s)
    return out


def locate_scenes_by_keyword(
    *,
    script_id: str,
    keywords: List[str],
    limit: int = 50,
    engine: Engine = default_engine,
) -> List[Scene]:
    """按关键词命中场景（关键词 OR 关系，使用 ILIKE）。

    给风险扫描的第一级关键词筛查使用。
    """
    if not keywords:
        return []
    # 构造 OR 条件 + 命名参数；避免 SQL 注入
    where_parts: List[str] = []
    params: dict = {"sid": script_id, "limit": limit}
    for i, kw in enumerate(keywords):
        if not kw:
            continue
        params[f"kw_{i}"] = f"%{kw}%"
        where_parts.append(f"text ILIKE :kw_{i}")
    if not where_parts:
        return []
    where_clause = " OR ".join(where_parts)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid AND ({where_clause})
                ORDER BY episode_no NULLS LAST, scene_no
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def locate_scenes_by_character(
    *,
    script_id: str,
    character: str,
    limit: int = 100,
    engine: Engine = default_engine,
) -> List[Scene]:
    """某角色出现的所有场景（按 characters 数组 + text 双路命中）。"""
    if not character:
        return []
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id::text AS id, script_id::text AS script_id,
                       episode_no, scene_no, scene_label, characters,
                       start_line, end_line, text
                FROM scriptlens.scenes
                WHERE script_id = :sid
                  AND (:name = ANY(characters) OR text ILIKE :name_like)
                ORDER BY episode_no NULLS LAST, scene_no
                LIMIT :limit
                """
            ),
            {"sid": script_id, "name": character, "name_like": f"%{character}%", "limit": limit},
        ).mappings().all()
    return [Scene(**dict(r)) for r in rows]


def get_scenes_around(
    *,
    script_id: str,
    target_scene_id: str,
    before: int = 5,
    after: int = 0,
    engine: Engine = default_engine,
) -> List[Scene]:
    """以 target_scene 为锚点取前后场景（上下文回扫用）。"""
    target = get_scene(scene_id=target_scene_id, engine=engine)
    if not target:
        return []
    all_scenes = get_all_scenes(script_id=script_id, engine=engine)
    idx = next((i for i, s in enumerate(all_scenes) if s.id == target.id), None)
    if idx is None:
        return [target]
    lo = max(0, idx - before)
    hi = min(len(all_scenes), idx + after + 1)
    return all_scenes[lo:hi]


_META_PREFIXES = ("人物", "出场", "角色", "登场", "场景", "时间", "地点", "时空")
_ACTION_MARKERS = ("▲", "△", "◇", "◆", "*")


def _is_meta_line(line: str) -> bool:
    """场景头/人物清单/位置行——这些是结构性元数据，不该作为 evidence quote。"""
    s = line.strip()
    if not s:
        return True
    # "人物：xxx"、"出场：xxx"、"地点：xxx"——形如 [关键词][：:]
    head = s[:4]
    for prefix in _META_PREFIXES:
        if s.startswith(prefix) and ("：" in head or ":" in head):
            return True
    # 数字场号头："1-2"、"第3集"、"第3集第2场"
    if any(s.startswith(p) for p in ("第",)):
        return True
    if s[:1].isdigit() and ("场" in s[:6] or "集" in s[:4] or s[1:2] in ("-", "－")):
        return True
    return False


def extract_quote(
    *,
    scene_id: str,
    max_chars: int = EVIDENCE_QUOTE_MAX_LEN,
    engine: Engine = default_engine,
) -> Optional[dict]:
    """提取场景里最显著的一段，作为 evidence 摘要片段。

    返回的 start_line / end_line 是 **scene.text 内的 1-indexed 物理行号**，
    与前端 Monaco 编辑器打开的内容（scene.text）严格对应——不要再用
    scenes.start_line/end_line（那是原始 paragraphs 数组下标，含空行/重组，
    与 scene.text 行号根本不对齐）。

    选择策略（每条都要求"非 meta 行"——跳过人物清单、场景头、位置）：
      1. 首条动作行（▲△ 开头）—— rubric 里"事件"通常在动作行
      2. 首条对白行（含「：」或「:」分隔，且冒号前是短人名）
      3. 首条非空非 meta 行
      4. 实在没有 → 截断 scene.text 前 max_chars 字（line=1）
    """
    scene = get_scene(scene_id=scene_id, engine=engine)
    if not scene or not scene.text:
        return None

    raw_lines = scene.text.split("\n")  # 不剔空行，保留物理行号

    quote: Optional[str] = None
    quote_line: Optional[int] = None  # 1-indexed

    # pass 1：动作行
    for idx, raw in enumerate(raw_lines):
        ln = raw.strip()
        if not ln or _is_meta_line(ln):
            continue
        if ln.startswith(_ACTION_MARKERS):
            quote, quote_line = ln, idx + 1
            break

    # pass 2：对白行（"角色名：台词"）—— 冒号前应是短人名（≤6 中文字符）
    if quote is None:
        for idx, raw in enumerate(raw_lines):
            ln = raw.strip()
            if not ln or _is_meta_line(ln):
                continue
            sep_idx = -1
            for sep in ("：", ":"):
                k = ln.find(sep)
                if k != -1:
                    sep_idx = k
                    break
            if sep_idx <= 0 or sep_idx > 12:
                continue
            speaker = ln[:sep_idx].strip()
            if 1 <= len(speaker) <= 6 and not _is_meta_line(speaker):
                quote, quote_line = ln, idx + 1
                break

    # pass 3：任何非 meta 实质行
    if quote is None:
        for idx, raw in enumerate(raw_lines):
            ln = raw.strip()
            if not ln or _is_meta_line(ln):
                continue
            quote, quote_line = ln, idx + 1
            break

    # pass 4：彻底兜底
    if quote is None:
        quote = scene.text.strip()
        quote_line = 1

    if len(quote) > max_chars:
        quote = quote[: max_chars - 1] + "…"

    return {
        "quote": quote,
        "scene_id": scene.id,
        "episode_no": scene.episode_no,
        "scene_no": scene.scene_no,
        "scene_label": scene.scene_label,
        "start_line": quote_line,
        "end_line": quote_line,
    }


def format_scene_for_llm(
    *,
    scene_text: str,
    max_chars: Optional[int] = None,
) -> str:
    """把场文本按行打 [L{n}] 行号标注，给 LLM 引用 evidence_line_range。

    输出格式（n 是 1-based 行号）：
        [L1] 5-1 客厅 日内
        [L2] 人物：宁卓 苏怀瑾 陈红梅 许杰
        [L3] ▲苏怀瑾赶上前抱住宁卓安抚...
        [L4] 许杰（气结）：你！你！宁卓...

    业内对照：Cursor codebase indexing / GitHub Copilot Workspace 都用这种
    "原文带行号 → LLM 引用 file:start-end" 的方式把锚点责任前置到 LLM，
    避免下游字符匹配反推（不稳定）。

    若 max_chars 给定，截断时尽量按行切（保持行号语义），并在末尾标 [TRUNCATED]。
    """
    if not scene_text:
        return ""
    raw_lines = scene_text.split("\n")
    if max_chars is None or sum(len(ln) for ln in raw_lines) + len(raw_lines) <= max_chars:
        return "\n".join(f"[L{i + 1}] {ln}" for i, ln in enumerate(raw_lines))

    out: List[str] = []
    used = 0
    for i, ln in enumerate(raw_lines):
        cell = f"[L{i + 1}] {ln}"
        if used + len(cell) + 1 > max_chars:
            out.append("[TRUNCATED]")
            break
        out.append(cell)
        used += len(cell) + 1
    return "\n".join(out)


def locate_quote_in_scene(
    *,
    scene_text: str,
    quote: str,
) -> Optional[tuple[int, int]]:
    """在 scene.text 里定位一段 LLM 给的 evidence 文本的 1-indexed 行号范围。

    返回 (start_line, end_line)；找不到返回 None（调用方应 fallback）。

    用于：风险/评分链路里 LLM 输出的 evidence 字段（要求是原文片段）
    需要被精确定位到行号，前端高亮才能跳到真正的论据所在行。

    匹配策略（递进 fallback）：
    1. 完全匹配 scene_text.find(quote) —— 字面对齐
    2. 行内子串匹配 —— 遍历 scene_text 每一行，找首条 contains(quote 前 30 字) 的行
    3. 前 12 字模糊匹配 —— LLM 改写或加省略号时退化为头部对齐
    """
    if not scene_text or not quote:
        return None
    raw_lines = scene_text.split("\n")

    pos = scene_text.find(quote)
    if pos != -1:
        before = scene_text[:pos]
        start = before.count("\n") + 1
        end = start + quote.count("\n")
        return (start, end)

    head = quote[:30].strip()
    if head:
        for idx, ln in enumerate(raw_lines):
            if head and head in ln:
                return (idx + 1, idx + 1)

    head_short = quote[:12].strip()
    if head_short:
        for idx, ln in enumerate(raw_lines):
            if head_short and head_short in ln:
                return (idx + 1, idx + 1)
    return None


def parse_line_range(
    raw: object,
    *,
    scene_line_count: int,
    max_span: int = 20,
) -> Optional[tuple[int, int]]:
    """LLM 输出的 line_range 解析与校验。

    - 接受 [start, end] / {"start": s, "end": e} / "L3-L9" / "3-9" 多种形态
    - 1-based 闭区间；start <= end
    - clamp 到 [1, scene_line_count]
    - 跨度超 max_span 时截到 max_span（防 LLM 给整场 1-99 大区间）
    - 解析失败返回 None（调用方应 fallback 到整场或 quote 反推）
    """
    if scene_line_count <= 0:
        return None

    start = end = None
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        try:
            start = int(raw[0])
            end = int(raw[1])
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, dict):
        try:
            start = int(raw.get("start"))  # type: ignore[arg-type]
            end = int(raw.get("end"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    elif isinstance(raw, str):
        s = raw.strip().lstrip("L").lstrip("l")
        if "-" not in s:
            return None
        try:
            a, b = s.split("-", 1)
            start = int(a.strip().lstrip("L").lstrip("l"))
            end = int(b.strip().lstrip("L").lstrip("l"))
        except (TypeError, ValueError):
            return None
    else:
        return None

    if start is None or end is None:
        return None
    if end < start:
        start, end = end, start
    start = max(1, min(start, scene_line_count))
    end = max(1, min(end, scene_line_count))
    if end - start + 1 > max_span:
        end = start + max_span - 1
    return (start, end)


def get_scene_id_by_no(
    *,
    script_id: str,
    scene_no: str,
    engine: Engine = default_engine,
) -> Optional[str]:
    """已知 scene_no 反查 scene_id（rubric 评分输出 evidence_ref_ids 时用）。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id::text AS id FROM scriptlens.scenes
                WHERE script_id = :sid AND scene_no = :sno
                LIMIT 1
                """
            ),
            {"sid": script_id, "sno": scene_no},
        ).first()
    return row.id if row else None
