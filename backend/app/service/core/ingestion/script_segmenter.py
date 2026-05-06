"""短剧剧本场景切分器。

输入：从 docx / pdf / txt / md 提取出的段落（行）列表。
输出：metadata 块 + 场景列表，每个场景含 episode_no / scene_no / scene_label /
characters / text。

设计依据见 eval/_probe_segmenter_out.txt 真实剧本观察：
- 集号：`第一集` | `01、` | `第1集`
- 场号：`1-1` | `1-2A` | 同行附带场景头 `1-1 房间 沈溪 傅立辉`
- 场景头：`场景：xxx，日，内` | `1-1 元家别墅门口日内`（连写）
- 人物行：`人物：A，B，C`
- 动作行：`▲` `△` 开头
- 对话行：`人物名：台词`

兜底（fallback）：完全没识别到场号 → 按集号切（粒度变粗）；连集号也没有 →
按固定段落窗口切（每 ~30 段一块），并在 warnings 中记录。
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
#   01、 / 1、
#   第1集 / 第10集
_EP_CHN_PAT = re.compile(r"^\s*第\s*([零一二三四五六七八九十百千两\d]+)\s*集\s*[A-Za-z]?\s*$")
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
    fallback_strategy: Optional[str] = None  # None | "episode_only" | "fixed_window"


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
        SegmentResult。当未识别到任何场号/集号时，使用 fixed_window 兜底切分。
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

    # 1. 拆分 metadata 块（前言/大纲/人物小传）和正片
    metadata_block, body_start = _extract_metadata_block(paragraphs, max_metadata_lookahead)

    # 2. 主循环：扫描正片，识别集号/场号
    scenes, total_eps, fallback = _scan_body(paragraphs, body_start, warnings)

    if not scenes:
        warnings.append("未识别到任何场号或集号，使用固定段落窗口兜底切分")
        scenes = _fallback_fixed_window(paragraphs, body_start)
        fallback = "fixed_window"
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

    判定：
      - 开头若干段中，遇到第一个集号或场号标记前的所有内容视为 metadata
      - 但 metadata 至少要包含一个已知 header（剧本大纲/人物小传等）才认定，
        否则直接 body_start = 0
    """
    scan_until = min(len(paragraphs), lookahead)
    body_start = -1
    has_metadata_header = False

    for i in range(scan_until):
        line = paragraphs[i].strip()
        if not line:
            continue
        # 一旦遇到集号或合法场号（排除大纲范围 "11-20："），正片开始
        if _is_episode_marker(line) or _is_valid_scene_marker(line) is not None:
            body_start = i
            break
        # 检测 metadata header
        for header in _METADATA_HEADERS:
            if header in line and len(line) < 30:
                has_metadata_header = True
                break

    if body_start < 0:
        # 整段扫完都没找到正片，全部视为 metadata
        return "\n".join(paragraphs), len(paragraphs)

    if not has_metadata_header or body_start == 0:
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
    """从 start_idx 起扫描正片。

    优先按场号切；若整篇仅有集号没有场号，回退为「每集一个 scene」。
    """
    # 先快速扫一遍：是否存在合法场号？
    has_scene_no = any(
        _is_valid_scene_marker(p.strip()) is not None for p in paragraphs[start_idx:]
    )
    has_episode = any(_is_episode_marker(p.strip()) for p in paragraphs[start_idx:])

    if has_scene_no:
        scenes, total_eps = _scan_by_scene_no(paragraphs, start_idx)
        return scenes, total_eps, None

    if has_episode:
        warnings.append("仅识别到集号、未识别到场号，每集作为一个 scene")
        scenes, total_eps = _scan_by_episode_only(paragraphs, start_idx)
        return scenes, total_eps, "episode_only"

    return [], 0, None


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
    scenes: List[ParsedScene] = []
    current_episode: Optional[int] = None
    episodes_seen: set[int] = set()

    cur_scene_no: Optional[str] = None
    cur_scene_episode: Optional[int] = None
    cur_scene_label_parts: List[str] = []
    cur_characters: List[str] = []
    cur_text_parts: List[str] = []
    cur_scene_start_idx: int = start_idx

    def flush(end_idx: int) -> None:
        if cur_scene_no is None:
            return
        text = "\n".join(p for p in cur_text_parts if p)
        scene_label = " ".join(p.strip() for p in cur_scene_label_parts if p.strip()).strip("、,，:： ")
        scenes.append(ParsedScene(
            episode_no=cur_scene_episode,
            scene_no=cur_scene_no,
            scene_label=scene_label,
            characters=list(cur_characters),
            text=text,
            start_idx=cur_scene_start_idx,
            end_idx=end_idx,
        ))

    last_idx = start_idx
    for i in range(start_idx, len(paragraphs)):
        last_idx = i
        line = paragraphs[i].strip()
        if not line:
            continue

        ep = _parse_episode_marker(line)
        if ep is not None:
            current_episode = ep
            episodes_seen.add(ep)
            continue

        marker = _is_valid_scene_marker(line)
        if marker is not None:
            ep_part, scene_part, suffix, tail = marker
            if current_episode is None:
                current_episode = ep_part
                episodes_seen.add(ep_part)
            flush(end_idx=i - 1)
            cur_scene_no = f"{ep_part}-{scene_part}{suffix}"
            cur_scene_episode = current_episode
            cur_scene_start_idx = i
            cur_scene_label_parts = [tail] if tail else []
            cur_characters = []
            cur_text_parts = []
            continue

        if cur_scene_no is None:
            continue

        m = _SCENE_LABEL_LINE_PAT.match(line)
        if m:
            cur_scene_label_parts.append(m.group(1).strip())
            continue
        m = _CHARACTERS_LINE_PAT.match(line)
        if m:
            cur_characters.extend(_split_characters(m.group(1)))
            continue
        m = _LOCATION_LINE_PAT.match(line)
        if m:
            cur_scene_label_parts.append(m.group(1).strip())
            continue
        m = _TIME_LINE_PAT.match(line)
        if m:
            cur_scene_label_parts.append(m.group(1).strip())
            continue

        cur_text_parts.append(line)
        if not _starts_with_action_marker(line):
            dm = _DIALOGUE_PAT.match(line)
            if dm:
                speaker = dm.group(1).strip()
                if 1 <= len(speaker) <= 6 and speaker not in cur_characters:
                    if not _looks_like_meta_keyword(speaker):
                        cur_characters.append(speaker)

    if cur_scene_no is not None:
        flush(end_idx=last_idx)

    return scenes, len(episodes_seen)


def _scan_by_episode_only(
    paragraphs: List[str],
    start_idx: int,
    *,
    max_chars_per_scene: int = 1500,
) -> tuple[List[ParsedScene], int]:
    """单集没有场号时，每集作为切分单元；超长集再按字数二次切窗口。"""
    scenes: List[ParsedScene] = []
    current_episode: Optional[int] = None
    cur_text_parts: List[str] = []
    cur_para_indices: List[int] = []
    cur_characters: List[str] = []
    episodes_seen: set[int] = set()

    def flush(end_idx: int) -> None:
        if current_episode is None or not cur_text_parts:
            return
        # 二次切：按字数窗口
        sub_idx = 0
        buf_text: List[str] = []
        buf_paras: List[int] = []
        buf_chars = 0
        for line, para_i in zip(cur_text_parts, cur_para_indices):
            if buf_chars + len(line) > max_chars_per_scene and buf_text:
                sub_idx += 1
                scenes.append(ParsedScene(
                    episode_no=current_episode,
                    scene_no=f"{current_episode}-{sub_idx}",
                    scene_label="",
                    characters=list(cur_characters),
                    text="\n".join(buf_text),
                    start_idx=buf_paras[0],
                    end_idx=buf_paras[-1],
                ))
                buf_text = []
                buf_paras = []
                buf_chars = 0
            buf_text.append(line)
            buf_paras.append(para_i)
            buf_chars += len(line)
        if buf_text:
            sub_idx += 1
            scenes.append(ParsedScene(
                episode_no=current_episode,
                scene_no=f"{current_episode}-{sub_idx}",
                scene_label="",
                characters=list(cur_characters),
                text="\n".join(buf_text),
                start_idx=buf_paras[0],
                end_idx=buf_paras[-1],
            ))

    for i in range(start_idx, len(paragraphs)):
        line = paragraphs[i].strip()
        if not line:
            continue
        ep = _parse_episode_marker(line)
        if ep is not None:
            flush(i - 1)
            current_episode = ep
            episodes_seen.add(ep)
            cur_text_parts = []
            cur_para_indices = []
            cur_characters = []
            continue
        if current_episode is None:
            continue
        cur_text_parts.append(line)
        cur_para_indices.append(i)
        if not _starts_with_action_marker(line):
            dm = _DIALOGUE_PAT.match(line)
            if dm:
                speaker = dm.group(1).strip()
                if 1 <= len(speaker) <= 6 and speaker not in cur_characters:
                    if not _looks_like_meta_keyword(speaker):
                        cur_characters.append(speaker)

    flush(len(paragraphs) - 1)
    return scenes, len(episodes_seen)


def _fallback_fixed_window(paragraphs: List[str], start_idx: int, window: int = 30) -> List[ParsedScene]:
    scenes: List[ParsedScene] = []
    body = [(i, p) for i, p in enumerate(paragraphs) if i >= start_idx and p.strip()]
    if not body:
        return scenes
    chunk_idx = 0
    for begin in range(0, len(body), window):
        sub = body[begin:begin + window]
        if not sub:
            break
        chunk_idx += 1
        text = "\n".join(p for _, p in sub)
        scenes.append(ParsedScene(
            episode_no=None,
            scene_no=f"f-{chunk_idx}",
            scene_label="",
            characters=[],
            text=text,
            start_idx=sub[0][0],
            end_idx=sub[-1][0],
        ))
    return scenes


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
