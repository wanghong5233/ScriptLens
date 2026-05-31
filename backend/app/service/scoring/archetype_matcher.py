"""题材原型 / 角色原型匹配器。

v1：关键词匹配（命中次数 + 互不相重的归一化）
v2（后续）：embedding cosine 相似度 + 关键词混合

设计要点：
- 所有 archetype 名称 / 关键词全部从 libraries/*.yaml 读，Python 文件不留任何业务字面量
- 输出 ArchetypeMatch.score ∈ [0, 1]，由调用方根据 signal_cfg.tier_anchor 映射分数

业内对照：
- ReelShort comparable archetype 使用 keyword + embedding 双轨；本期先关键词，
  embedding 接口在 match_archetypes(embed_fn=...) 上预留
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional

from service.scoring.rubric_loader import load_archetype_library


@dataclass
class ArchetypeEntry:
    id: str
    name: str
    aliases: tuple[str, ...]
    signature_keywords: tuple[str, ...]
    negative_keywords: tuple[str, ...] = ()
    typical_role: Optional[str] = None
    reference: str = ""


@dataclass
class ArchetypeMatch:
    archetype: ArchetypeEntry
    score: float                       # 0.0 - 1.0
    hit_keywords: tuple[str, ...] = ()


@lru_cache(maxsize=8)
def _load_archetype_entries(library_name: str) -> tuple[ArchetypeEntry, ...]:
    data = load_archetype_library(library_name)
    entries: list[ArchetypeEntry] = []
    for item in data.get("archetypes", []):
        entries.append(
            ArchetypeEntry(
                id=item.get("id", ""),
                name=item.get("name", ""),
                aliases=tuple(item.get("aliases", []) or []),
                signature_keywords=tuple(item.get("signature_keywords", []) or []),
                negative_keywords=tuple(item.get("negative_keywords", []) or []),
                typical_role=item.get("typical_role"),
                reference=item.get("reference", ""),
            )
        )
    return tuple(entries)


def match_genre_archetype(
    text: str,
    library_name: str = "archetypes_cn",
    *,
    embed_fn: Callable[[str], Any] | None = None,
) -> list[ArchetypeMatch]:
    """全剧文本（或前 N 集拼接）vs 题材原型库。

    返回按 score 降序排序的所有 match（score>0）。
    embed_fn 留作 v2 embedding 接入位，当前忽略。
    """
    del embed_fn  # 占位
    entries = _load_archetype_entries(library_name)
    text_norm = text or ""
    results: list[ArchetypeMatch] = []
    for entry in entries:
        score, hits = _score_keywords(text_norm, entry)
        if score > 0:
            results.append(ArchetypeMatch(archetype=entry, score=score, hit_keywords=tuple(hits)))
    results.sort(key=lambda m: m.score, reverse=True)
    return results


def match_character_archetype(
    character_names_and_traits: list[str],
    library_name: str = "character_archetypes_cn",
) -> list[tuple[str, ArchetypeMatch]]:
    """对每个角色 (name + traits 描述拼接) 找 top-1 匹配。

    返回 [(input_text, ArchetypeMatch), ...]，匹配不到的角色不包含在结果中。
    """
    entries = _load_archetype_entries(library_name)
    out: list[tuple[str, ArchetypeMatch]] = []
    for text in character_names_and_traits:
        best: Optional[ArchetypeMatch] = None
        for entry in entries:
            score, hits = _score_keywords(text or "", entry)
            if score <= 0:
                continue
            match = ArchetypeMatch(archetype=entry, score=score, hit_keywords=tuple(hits))
            if best is None or match.score > best.score:
                best = match
        if best is not None:
            out.append((text, best))
    return out


def _score_keywords(text: str, entry: ArchetypeEntry) -> tuple[float, list[str]]:
    """关键词命中评分。

    分子：sig_keyword 命中数 + 0.5 * alias 命中数 - neg_keyword 命中数
    分母：归一化系数 = sig_keyword 总数（不含 alias）
    score ∈ [0, 1]，clamp。
    """
    if not text:
        return 0.0, []
    hits: list[str] = []
    pos = 0.0
    for kw in entry.signature_keywords:
        if kw and kw in text:
            pos += 1.0
            hits.append(kw)
    for alias in entry.aliases:
        if alias and alias in text:
            pos += 0.5
            hits.append(alias)
    neg = 0.0
    for kw in entry.negative_keywords:
        if kw and kw in text:
            neg += 1.0
    raw = pos - neg
    denom = max(len(entry.signature_keywords), 1)
    score = max(0.0, min(1.0, raw / denom))
    return score, hits


__all__ = [
    "ArchetypeEntry",
    "ArchetypeMatch",
    "match_character_archetype",
    "match_genre_archetype",
]
