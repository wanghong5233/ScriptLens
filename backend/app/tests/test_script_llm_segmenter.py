"""script_llm_segmenter 单测：聚焦 ``_coerce_to_parsed_scenes`` 的边界严格性。

LLM 兜底切场是错切比不切更糟的场景——错位边界会传染下游评分 / 关系图 /
高光等所有 LLM 链路。本套测试锁死"任何重叠 / 漏段 / 越界都整体拒绝"
的契约，避免 LLM 偶尔输出"看似合理但有缺陷"的切分被静默吞下。
"""

from __future__ import annotations

from service.script_tools import script_llm_segmenter as ls


_BODY = [
    "[paragraph 0]",
    "[paragraph 1]",
    "[paragraph 2]",
    "[paragraph 3]",
    "[paragraph 4]",
    "[paragraph 5]",
]


def test_coerce_normal_split_produces_correct_scenes() -> None:
    raw = [
        {"start_para": 0, "end_para": 2, "scene_label": "客厅 日内", "characters": ["A", "B"]},
        {"start_para": 3, "end_para": 5, "scene_label": "车上回忆", "characters": ["A"]},
    ]
    out = ls._coerce_to_parsed_scenes(
        raw, body_paragraphs=_BODY, body_start_in_full=10
    )
    assert out is not None
    assert len(out) == 2
    assert out[0].scene_no == "L1"
    assert out[0].scene_label == "客厅 日内"
    assert out[0].characters == ["A", "B"]
    assert out[0].start_idx == 10  # body_start_in_full + 0
    assert out[0].end_idx == 12    # body_start_in_full + 2
    assert out[1].start_idx == 13


def test_coerce_rejects_overlap() -> None:
    raw = [
        {"start_para": 0, "end_para": 3, "scene_label": "A", "characters": []},
        {"start_para": 2, "end_para": 5, "scene_label": "B", "characters": []},  # 与第一场 [2,3] 重叠
    ]
    out = ls._coerce_to_parsed_scenes(raw, body_paragraphs=_BODY, body_start_in_full=0)
    assert out is None


def test_coerce_rejects_out_of_range() -> None:
    raw = [
        {"start_para": 0, "end_para": 9, "scene_label": "X", "characters": []},  # ep=9 越界
    ]
    out = ls._coerce_to_parsed_scenes(raw, body_paragraphs=_BODY, body_start_in_full=0)
    assert out is None


def test_coerce_rejects_missing_paragraphs() -> None:
    """漏段会丢失原文，必须整体拒绝（保零丢失契约）。"""
    raw = [
        {"start_para": 0, "end_para": 1, "scene_label": "A", "characters": []},
        # 漏掉 [2, 3]
        {"start_para": 4, "end_para": 5, "scene_label": "B", "characters": []},
    ]
    out = ls._coerce_to_parsed_scenes(raw, body_paragraphs=_BODY, body_start_in_full=0)
    assert out is None


def test_coerce_rejects_only_one_scene() -> None:
    """只切一场等于没切：浪费 LLM 调用，应保留 single_scene。"""
    raw = [
        {"start_para": 0, "end_para": 5, "scene_label": "整篇", "characters": []},
    ]
    out = ls._coerce_to_parsed_scenes(raw, body_paragraphs=_BODY, body_start_in_full=0)
    assert out is None


def test_coerce_caps_too_many_scenes() -> None:
    """过碎切分（>30 场）会被裁到上限，但前提是覆盖完整。"""
    body = [f"para {i}" for i in range(60)]
    raw = [
        {"start_para": i * 2, "end_para": i * 2 + 1, "scene_label": f"s{i}", "characters": []}
        for i in range(30)
    ]
    out = ls._coerce_to_parsed_scenes(raw, body_paragraphs=body, body_start_in_full=0)
    assert out is not None
    assert len(out) == ls.LLM_SEGMENT_MAX_SCENES
