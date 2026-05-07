"""短剧剧本场景切分器。

输入：从 docx / pdf / txt / md 提取出的段落（行）列表。
输出：metadata 块 + 场景列表，每个场景含 episode_no / scene_no / scene_label /
characters / text。

切分原则（产品级，零丢失）：
1. 按剧本天然层级 集 → 场 切，不按字数。
2. 集号头之间的所有非空段落必属于该集；场号 marker 之间的内容必属于该场。
3. 每集内部的切场策略按优先级：
   a) 数字场号（`1-1`、`1-2A`）— 最权威
   b) 裸场景头（`客厅 日内`、`车内，夜` 等含日/夜/内/外的短行）— 行业常见简写
   c) 整集作为单场 — 兜底，绝不再做字数二次切
4. 集号头缺失但有数字场号 → 按场号 ep_part 回填集号。
5. 完全无任何标记 → 整篇作为单场（保留全部内容）。

设计依据来自 eval/_probe_segmenter_out.txt 多本真实剧本：
- 集号：`第一集` | `第一集：` | `第1集` | `01、` | `第10回` | `第10话`
- 场号：`1-1` | `1-2A` | `1-1 房间 沈溪 傅立辉`
- 裸场景头：`纯黑环境 夜内` | `二层客厅 日内` | `车内，夜`
- 场景头：`场景：xxx，日，内`
- 人物行：`人物：A，B，C`
- 动作行：`▲` `△` 开头
- 对话行：`人物名：台词`
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 正则表达式
# ============================================================

# 集号识别（全段匹配）：
#   第一集 / 第二集 / 第十集 / 第100集
#   第一集： / 第一集. / 第一集，   ← 末尾允许中英标点（真实剧本常见）
#   01、 / 1、
#   第1集 / 第10集 / 第10回 / 第10话
# 末尾可选标点集合：: ：、，,。．. ;；空白；同时容忍单个英文字母后缀（如「第1集A」）。
_EP_TAIL_PUNCT = r"[\s:：、，,。．.;；]*"
_EP_CHN_PAT = re.compile(
    rf"^\s*第\s*([零一二三四五六七八九十百千两\d]+)\s*[集回话]\s*[A-Za-z]?\s*{_EP_TAIL_PUNCT}$"
)
_EP_NUM_PAT = re.compile(r"^\s*(\d{1,4})\s*[、,，.．]\s*$")

# 场号识别：1-1 / 1-2A / 1-1 后跟场景头
# 必须以场号开头，后面可选分隔符（、,，：:），再跟场景头/人物
_SCENE_NO_PAT = re.compile(
    r"^\s*(\d{1,3})\s*-\s*(\d{1,3})\s*([A-Za-z])?\s*[、,，：:．\.]?\s*(.*)$"
)

# 场号的合法范围（短剧业界）：集 ≤200，场 ≤50
_MAX_EPISODE_NO = 200
_MAX_SCENE_NO = 50

# tail 太长 → 大纲描述伪装成 N-M（如 "11-20：看到安然虚弱..."）
_SCENE_TAIL_MAX_LEN = 30

# 场景信息行
_SCENE_LABEL_LINE_PAT = re.compile(r"^\s*场景\s*[:：]\s*(.+)$")
_CHARACTERS_LINE_PAT = re.compile(r"^\s*人物\s*[:：]\s*(.+)$")
_LOCATION_LINE_PAT = re.compile(r"^\s*地点\s*[:：]\s*(.+)$")
_TIME_LINE_PAT = re.compile(r"^\s*时间\s*[:：]\s*(.+)$")

# 人物对话行：「林茵：" 台词"」或「林茵：台词」
# 只匹配开头 1-6 个字（符合中文人名常见长度，宽松一些为了 OS、画外音等）
_DIALOGUE_PAT = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z0-9\s]{1,8})\s*[:：]\s*(.+)$")

# 动作/旁白行
_ACTION_PREFIX = ("▲", "△", "◇", "◆")

# 裸场景头识别：典型样式如「纯黑环境 夜内」「二层客厅 日内」「车内，夜」「公司大堂 日」
# 形态：短行（≤ 30 字符）+ 含 内/外/日/夜/早/晚/晨 等时空关键词 + 不含冒号
# 关键词组合（按区分度排序）：
#   优先匹配「日内/夜内/日外/夜外/内景/外景/内/外」等连写
#   其次匹配以单字「日|夜|外|内|早|晚|晨」结尾且前面有空白/逗号分隔
_BARE_HEADING_KEYWORDS = re.compile(
    r"(日内|夜内|日外|夜外|晨内|晨外|"
    r"内景|外景|内\s*/\s*外|外\s*/\s*内|"
    r"清晨|傍晚|黎明|凌晨|早晨|上午|中午|下午|晚上|深夜|半夜|"
    r"[，,\s](日|夜|外|内|早|晚|晨|暮)$)"
)
_BARE_HEADING_MAX_LEN = 30

# Metadata 段头识别（前言/大纲/人物小传）
_METADATA_HEADERS = ("剧本大纲", "人物小传", "大纲", "人设", "故事梗概", "简介",
                     "基本信息", "剧情简介", "分集大纲", "剧集大纲")

# 中文数字 → 阿拉伯
_CHN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100,
    "千": 1000, "两": 2,
}


def _parse_chn_or_int(text: str) -> Optional[int]:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    # 中文数字：支持 "一" "十" "二十" "二十一" "一百" "一百零五"
    if all(ch in _CHN_NUM for ch in text):
        # 简化算法：足够覆盖 1-999
        result = 0
        section = 0
        for ch in text:
            n = _CHN_NUM[ch]
            if n >= 100:
                section = max(section, 1) * n
                result += section
                section = 0
            elif n == 10:
                section = max(section, 1) * 10
            else:
                section += n
        return result + section
    return None


# ============================================================
# 数据类
# ============================================================


@dataclass
class ParsedScene:
    episode_no: Optional[int]
    scene_no: str
    scene_label: str
    characters: List[str]
    text: str
    start_idx: int
    end_idx: int


@dataclass
class SegmentResult:
    metadata_block: str
    scenes: List[ParsedScene]
    total_episodes: int
    total_scenes: int
    total_chars: int
    parsing_warnings: List[str] = field(default_factory=list)
    # None | "episode_only" | "bare_heading" | "single_scene"
    # 不再有 "fixed_window"——产品级方案不按字数切。
    fallback_strategy: Optional[str] = None


# ============================================================
# 主入口
# ============================================================


def segment_script(paragraphs: List[str], *, max_metadata_lookahead: int = 200) -> SegmentResult:
    """切分剧本段落。

    Args:
        paragraphs: 已剔除空白的段落列表（来自 docx / pdf / txt 提取）
        max_metadata_lookahead: 最多扫描前 N 段去检测 metadata 块；超过后强制
            进入正片识别

    Returns:
        SegmentResult。零丢失：每个非空段落（除 metadata）都进入某个 scene 的 text。
    """
    if not paragraphs:
        return SegmentResult(
            metadata_block="",
            scenes=[],
            total_episodes=0,
            total_scenes=0,
            total_chars=0,
            parsing_warnings=["输入为空"],
        )

    warnings: List[str] = []

    metadata_block, body_start = _extract_metadata_block(paragraphs, max_metadata_lookahead)

    scenes, total_eps, fallback = _scan_body(paragraphs, body_start, warnings)

    if not scenes:
        # 完全没识别到任何结构信息：整篇正文作为单场，零丢失。
        warnings.append("未识别到任何集号/场号/裸场景头，整篇作为单场承载")
        scenes = _fallback_single_scene(paragraphs, body_start)
        fallback = "single_scene"
        total_eps = 0

    total_chars = sum(len(s.text) for s in scenes)
    return SegmentResult(
        metadata_block=metadata_block,
        scenes=scenes,
        total_episodes=total_eps,
        total_scenes=len(scenes),
        total_chars=total_chars,
        parsing_warnings=warnings,
        fallback_strategy=fallback,
    )


# ============================================================
# Metadata 抽取
# ============================================================


def _extract_metadata_block(paragraphs: List[str], lookahead: int) -> tuple[str, int]:
    """识别开头的 metadata 块（大纲/人物小传），返回 (metadata_text, body_start_idx)。

    产品级语义：第一个集号头/数字场号 marker 之前的所有非空段落都是 metadata。
    简介、人物设定、人物小传段经常是长段落，不应被 `len(line) < 30` 类硬阈值漏判；
    `_METADATA_HEADERS` 仅作 hint，不再作为强制条件——marker 位置本身就是
    足够强的边界信号。

    若整篇没有任何 marker，返回空 metadata（body_start = 0），交由 fallback
    `_fallback_single_scene` 整篇承载，确保零丢失。
    """
    scan_until = min(len(paragraphs), lookahead)
    body_start = -1

    for i in range(scan_until):
        line = paragraphs[i].strip()
        if not line:
            continue
        if _is_episode_marker(line) or _is_valid_scene_marker(line) is not None:
            body_start = i
            break

    if body_start <= 0:
        return "", 0

    metadata_text = "\n".join(p for p in paragraphs[:body_start] if p.strip())
    return metadata_text, body_start


def _is_episode_marker(line: str) -> bool:
    if _EP_CHN_PAT.match(line):
        return True
    if _EP_NUM_PAT.match(line):
        # 排除场号「1-1」类（场号有连字符）
        if "-" not in line:
            return True
    return False


def _parse_episode_marker(line: str) -> Optional[int]:
    m = _EP_CHN_PAT.match(line)
    if m:
        return _parse_chn_or_int(m.group(1))
    m = _EP_NUM_PAT.match(line)
    if m and "-" not in line:
        return int(m.group(1))
    return None


# ============================================================
# 正片扫描
# ============================================================


def _scan_body(
    paragraphs: List[str],
    start_idx: int,
    warnings: List[str],
) -> tuple[List[ParsedScene], int, Optional[str]]:
    """两层扫描：先按集号头切集，再在每集内独立切场。

    Returns:
        (scenes, total_episodes, fallback_strategy)
        fallback_strategy: None | "episode_only" | "bare_heading"
    """
    # Pass 1: 收集集号头索引（按出现顺序）
    ep_marker_idxs: list[tuple[int, int]] = []  # [(idx, ep_no)]
    for i in range(start_idx, len(paragraphs)):
        line = paragraphs[i].strip()
        if not line:
            continue
        if _is_episode_marker(line):
            ep_no = _parse_episode_marker(line)
            if ep_no is not None:
                ep_marker_idxs.append((i, ep_no))

    has_scene_no = any(
        _is_valid_scene_marker(p.strip()) is not None for p in paragraphs[start_idx:]
    )

    # Pass 1.5: 集号头缺失但场号 ep_part 已出现 → 在该 ep_part 第一次出现位置补虚拟集号头
    # 真实剧本里偶有作者忘写「第N集」、直接以「N-1」开场的情况（如闪婚剧本第 47 集）
    if ep_marker_idxs and has_scene_no:
        seen_eps = {ep for _, ep in ep_marker_idxs}
        virtual_inserts: list[tuple[int, int]] = []
        observed_in_pass = set(seen_eps)
        for i in range(start_idx, len(paragraphs)):
            line = paragraphs[i].strip()
            if not line:
                continue
            m = _is_valid_scene_marker(line)
            if m is None:
                continue
            ep_part = m[0]
            if ep_part not in observed_in_pass:
                virtual_inserts.append((i, ep_part))
                observed_in_pass.add(ep_part)
        if virtual_inserts:
            ep_marker_idxs = sorted(ep_marker_idxs + virtual_inserts, key=lambda x: x[0])
            warnings.append(
                f"{len(virtual_inserts)} 个集号头缺失，已按场号 ep_part 自动补全"
            )

    if not ep_marker_idxs:
        # 无集号头：唯一可信的层级是数字场号（自带 ep_part）。
        if has_scene_no:
            scenes, total_eps = _scan_by_scene_no(paragraphs, start_idx)
            if total_eps >= 2:
                warnings.append(
                    f"未识别到「第N集」标记，集数 {total_eps} 来自场号 ep_part 回填"
                )
            return scenes, total_eps, None
        # 完全无集号、无数字场号 → 上层走 single_scene 兜底
        return [], 0, None

    # 有集号头：按集切片，每集内独立选切场策略
    scenes: List[ParsedScene] = []
    fallback_used: set[str] = set()

    # 每集的范围 [body_start, body_end)
    for k, (ep_idx, ep_no) in enumerate(ep_marker_idxs):
        header_line = paragraphs[ep_idx].strip() if 0 <= ep_idx < len(paragraphs) else ""
        is_real_header = _is_episode_marker(header_line)
        # 虚拟集号头：ep_idx 指向该集第一个数字场号 marker（如 `47-1 书房…`），
        # 该行本身就是首场的起点，不能跳过、不能 prepend（会重复）。
        body_begin = ep_idx + 1 if is_real_header else ep_idx
        body_end = ep_marker_idxs[k + 1][0] if k + 1 < len(ep_marker_idxs) else len(paragraphs)
        ep_scenes, strategy = _segment_one_episode(paragraphs, body_begin, body_end, ep_no)
        # 真实集号头（如「第一集」「第一集：」）原文 prepend 到该集第一场，零丢失
        if is_real_header and header_line and ep_scenes:
            first = ep_scenes[0]
            ep_scenes[0] = ParsedScene(
                episode_no=first.episode_no,
                scene_no=first.scene_no,
                scene_label=first.scene_label,
                characters=first.characters,
                text=header_line + "\n" + first.text if first.text else header_line,
                start_idx=ep_idx,
                end_idx=first.end_idx,
            )
        elif is_real_header and header_line and not ep_scenes:
            # 集号头之后完全没内容，仍保留一场承载头本身
            ep_scenes = [ParsedScene(
                episode_no=ep_no,
                scene_no=f"{ep_no}-1",
                scene_label="",
                characters=[],
                text=header_line,
                start_idx=ep_idx,
                end_idx=ep_idx,
            )]
        scenes.extend(ep_scenes)
        if strategy != "scene_no":
            fallback_used.add(strategy)

    # total_episodes 取 distinct episode_no 数量，避免虚拟集号头与真实集号头重复计数
    total_eps = len({s.episode_no for s in scenes if s.episode_no is not None})

    # 一致性 / 兜底告警
    if has_scene_no:
        scene_no_eps = {s.episode_no for s in scenes if s.scene_no and "-" in s.scene_no
                        and not s.scene_no.startswith("ep")}
        if scene_no_eps and abs(len(scene_no_eps) - total_eps) >= 2:
            warnings.append(
                f"集号头识别数={total_eps} 与含数字场号的集数={len(scene_no_eps)} 不一致，"
                f"请检查剧本集号格式"
            )
    if "bare_heading" in fallback_used:
        warnings.append("部分集没有数字场号，已按裸场景头（含日/夜/内/外）切分")
    if "episode_whole" in fallback_used:
        warnings.append("部分集既无数字场号也无裸场景头，已整集作为单场承载")

    fallback = None
    if fallback_used == {"episode_whole"}:
        fallback = "episode_only"
    elif "bare_heading" in fallback_used:
        fallback = "bare_heading"
    return scenes, total_eps, fallback


def _is_valid_scene_marker(line: str) -> Optional[tuple[int, int, str, str]]:
    """启发式判断该行是否为合法场号；返回 (ep, scene, suffix, tail) 或 None。

    拒绝规则：
      - tail 以「：」「:」开头 → 大纲行（如 "11-20：看到..."）
      - tail 长度 > _SCENE_TAIL_MAX_LEN → 大纲段落
      - ep / scene 越界
    """
    if _is_episode_marker(line):
        return None
    m = _SCENE_NO_PAT.match(line)
    if not m:
        return None
    ep_part = int(m.group(1))
    scene_part = int(m.group(2))
    suffix = m.group(3) or ""
    tail = (m.group(4) or "").strip()
    if ep_part < 1 or ep_part > _MAX_EPISODE_NO:
        return None
    if scene_part < 1 or scene_part > _MAX_SCENE_NO:
        return None
    # 原始行（去掉场号前缀后）若以 ":" 开头，说明是 "11-20：xxx" 范围描述
    raw_after_prefix = line.split(suffix if suffix else f"{ep_part}-{scene_part}", 1)[-1].lstrip()
    if raw_after_prefix.startswith(("：", ":")):
        return None
    if len(tail) > _SCENE_TAIL_MAX_LEN:
        return None
    return ep_part, scene_part, suffix, tail


def _scan_by_scene_no(paragraphs: List[str], start_idx: int) -> tuple[List[ParsedScene], int]:
    """无集号头但有数字场号时使用。零丢失：marker 之间所有非空段落都进 text。

    场号自带 ep_part，权威性高于集号头：集号头可能因为格式异常被漏识别，
    只要场号 ep 切换（如 1-x → 2-1）就以场号 ep_part 为准回填集号。
    """
    markers: list[tuple] = []
    for i in range(start_idx, len(paragraphs)):
        line = paragraphs[i].strip()
        if not line:
            continue
        m = _is_valid_scene_marker(line)
        if m is not None:
            markers.append((i,) + m)

    if not markers:
        return [], 0

    scenes: List[ParsedScene] = []
    episodes_seen: set[int] = set()
    body_end = len(paragraphs)

    for k, marker in enumerate(markers):
        m_idx, ep_part, scene_part, suffix, tail = marker
        next_idx = markers[k + 1][0] if k + 1 < len(markers) else body_end
        episodes_seen.add(ep_part)
        scene_lines = [paragraphs[i] for i in range(m_idx, next_idx) if paragraphs[i].strip()]
        label, characters, text_parts = _parse_scene_inner([tail] if tail else [], scene_lines)
        scenes.append(ParsedScene(
            episode_no=ep_part,
            scene_no=f"{ep_part}-{scene_part}{suffix}",
            scene_label=label,
            characters=characters,
            text="\n".join(text_parts),
            start_idx=m_idx,
            end_idx=next_idx - 1,
        ))
    return scenes, len(episodes_seen)


def _segment_one_episode(
    paragraphs: List[str],
    body_begin: int,
    body_end: int,
    ep_no: int,
) -> tuple[List[ParsedScene], str]:
    """切一集：数字场号 → 裸场景头 → 整集为一场。返回 (scenes, strategy)。"""
    scene_no_markers: list[tuple] = []
    for i in range(body_begin, body_end):
        line = paragraphs[i].strip()
        if not line:
            continue
        m = _is_valid_scene_marker(line)
        if m is not None:
            scene_no_markers.append((i,) + m)

    if scene_no_markers:
        return _split_episode_by_scene_no(
            paragraphs, body_begin, body_end, ep_no, scene_no_markers
        ), "scene_no"

    bare_markers: list[tuple[int, str]] = []
    for i in range(body_begin, body_end):
        line = paragraphs[i].strip()
        if not line:
            continue
        if _is_bare_scene_heading(line):
            bare_markers.append((i, line))

    if bare_markers:
        return _split_episode_by_bare(
            paragraphs, body_begin, body_end, ep_no, bare_markers
        ), "bare_heading"

    # 整集作为一场（绝不按字数切）
    whole = _whole_episode_as_one(paragraphs, body_begin, body_end, ep_no)
    return whole, "episode_whole"


def _split_episode_by_scene_no(
    paragraphs: List[str],
    body_begin: int,
    body_end: int,
    ep_no: int,
    markers: list[tuple],
) -> List[ParsedScene]:
    scenes: List[ParsedScene] = []
    first_idx = markers[0][0]
    # 集号头与首个 marker 之间的段落（如「出场人员: ...」）合并到第一场前
    prefix_lines = [paragraphs[i] for i in range(body_begin, first_idx) if paragraphs[i].strip()]

    for k, marker in enumerate(markers):
        m_idx, ep_part, scene_part, suffix, tail = marker
        next_idx = markers[k + 1][0] if k + 1 < len(markers) else body_end
        scene_lines = [paragraphs[i] for i in range(m_idx, next_idx) if paragraphs[i].strip()]
        label, characters, text_parts = _parse_scene_inner([tail] if tail else [], scene_lines)
        if k == 0 and prefix_lines:
            text_parts = prefix_lines + text_parts
        scenes.append(ParsedScene(
            episode_no=ep_no,
            scene_no=f"{ep_part}-{scene_part}{suffix}",
            scene_label=label,
            characters=characters,
            text="\n".join(text_parts),
            start_idx=body_begin if (k == 0 and prefix_lines) else m_idx,
            end_idx=next_idx - 1,
        ))
    return scenes


def _split_episode_by_bare(
    paragraphs: List[str],
    body_begin: int,
    body_end: int,
    ep_no: int,
    markers: list[tuple[int, str]],
) -> List[ParsedScene]:
    scenes: List[ParsedScene] = []
    first_idx = markers[0][0]
    prefix_lines = [paragraphs[i] for i in range(body_begin, first_idx) if paragraphs[i].strip()]

    for k, (m_idx, heading_text) in enumerate(markers):
        next_idx = markers[k + 1][0] if k + 1 < len(markers) else body_end
        scene_lines = [paragraphs[i] for i in range(m_idx, next_idx) if paragraphs[i].strip()]
        label, characters, text_parts = _parse_scene_inner([heading_text], scene_lines)
        if k == 0 and prefix_lines:
            text_parts = prefix_lines + text_parts
        scenes.append(ParsedScene(
            episode_no=ep_no,
            scene_no=f"{ep_no}-{k + 1}",
            scene_label=label,
            characters=characters,
            text="\n".join(text_parts),
            start_idx=body_begin if (k == 0 and prefix_lines) else m_idx,
            end_idx=next_idx - 1,
        ))
    return scenes


def _whole_episode_as_one(
    paragraphs: List[str],
    body_begin: int,
    body_end: int,
    ep_no: int,
) -> List[ParsedScene]:
    body_lines = [paragraphs[i] for i in range(body_begin, body_end) if paragraphs[i].strip()]
    if not body_lines:
        return []
    label, characters, text_parts = _parse_scene_inner([], body_lines)
    return [ParsedScene(
        episode_no=ep_no,
        scene_no=f"{ep_no}-1",
        scene_label=label,
        characters=characters,
        text="\n".join(text_parts),
        start_idx=body_begin,
        end_idx=body_end - 1,
    )]


def _parse_scene_inner(
    label_seed: List[str],
    body_lines: List[str],
) -> tuple[str, List[str], List[str]]:
    """从场内段落抽 label/characters；text_parts 镜像 body_lines（零丢失）。

    label/characters 仅为辅助索引，不会从 text 中删除原文。
    marker 行（集号/数字场号/裸场景头）不参与 label/characters 抽取，
    但其原文仍保留在 text_parts。
    """
    label_parts = list(label_seed)
    characters: List[str] = []
    text_parts: List[str] = list(body_lines)

    for line in body_lines:
        if _is_episode_marker(line):
            continue
        if _is_valid_scene_marker(line) is not None:
            continue
        if _is_bare_scene_heading(line):
            continue
        m = _SCENE_LABEL_LINE_PAT.match(line)
        if m:
            label_parts.append(m.group(1).strip())
            continue
        m = _CHARACTERS_LINE_PAT.match(line)
        if m:
            for c in _split_characters(m.group(1)):
                if c not in characters:
                    characters.append(c)
            continue
        m = _LOCATION_LINE_PAT.match(line)
        if m:
            label_parts.append(m.group(1).strip())
            continue
        m = _TIME_LINE_PAT.match(line)
        if m:
            label_parts.append(m.group(1).strip())
            continue
        if _starts_with_action_marker(line):
            continue
        dm = _DIALOGUE_PAT.match(line)
        if dm:
            speaker = dm.group(1).strip()
            if 1 <= len(speaker) <= 6 and speaker not in characters:
                if not _looks_like_meta_keyword(speaker):
                    characters.append(speaker)

    label = " ".join(p.strip() for p in label_parts if p.strip()).strip("、,，:： ")
    return label, characters, text_parts


def _is_bare_scene_heading(line: str) -> bool:
    """裸场景头：含 日/夜/内/外/早/晚/晨 等时空关键词的短行。

    用于覆盖剧本作者省略 `X-Y` 编号、直接以「客厅 日内」「车内，夜」开场的情况。
    """
    line = line.strip()
    if not line or len(line) > _BARE_HEADING_MAX_LEN:
        return False
    if _starts_with_action_marker(line):
        return False
    if _is_episode_marker(line):
        return False
    if _is_valid_scene_marker(line) is not None:
        return False
    # 标注/对话行带冒号；裸场景头不应有冒号
    if "：" in line or ":" in line:
        return False
    return bool(_BARE_HEADING_KEYWORDS.search(line))


def _fallback_single_scene(paragraphs: List[str], start_idx: int) -> List[ParsedScene]:
    """完全无任何结构信息：整篇正文作为单场，零丢失。"""
    body_lines = [paragraphs[i] for i in range(start_idx, len(paragraphs)) if paragraphs[i].strip()]
    if not body_lines:
        return []
    label, characters, text_parts = _parse_scene_inner([], body_lines)
    return [ParsedScene(
        episode_no=None,
        scene_no="1-1",
        scene_label=label,
        characters=characters,
        text="\n".join(text_parts),
        start_idx=start_idx,
        end_idx=len(paragraphs) - 1,
    )]


# ============================================================
# 工具
# ============================================================


def _split_characters(raw: str) -> List[str]:
    parts = re.split(r"[、,，;；/\s]+", raw.strip())
    return [p for p in parts if p and len(p) <= 8]


def _starts_with_action_marker(line: str) -> bool:
    return any(line.startswith(m) for m in _ACTION_PREFIX)


_META_KEYWORDS = {"场景", "人物", "地点", "时间", "字幕", "画外音", "OS", "os", "Os"}


def _looks_like_meta_keyword(speaker: str) -> bool:
    return speaker in _META_KEYWORDS
