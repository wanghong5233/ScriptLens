"""W3C TextQuoteSelector 重锚定单测（reward / risk / coverage 共用基础设施）。

覆盖契约：
- LLM 给的 verbatim `exact` 一字不差出现在 scene → 正确反算行号
- 归一化（去空白/标点）后命中 → 仍能反算
- 多次出现 + prefix/suffix 消歧 → 唯一定位
- 多次出现 + 无 prefix/suffix → fail-closed (None)
- 不命中 → None
- 极短 quote (< 4 字) → None（防误锚）
- 跨行 quote → 行号区间正确
"""
from __future__ import annotations

from service.script_tools.scene_repo import reconcile_text_quote_selector


_SCENE = """\
第六十九集
69-1 姜栀枝家夜内
人物：姜栀枝陆斯言顾聿之
顾聿之：你在里面吗。
△陆斯言把裤子提了上来
陆斯言：大小姐，他要是打我，把我当小三，你会保护我吗？
姜栀枝：你闭嘴。
姜栀枝：聿之哥哥，怎么了吗？
顾聿之（眼神犀利）：里面有人？
姜栀枝：没有啊，我房间的浴室怎么会有人呢？
△顾聿之摸住门把手，突然打开，冲进浴室"""


def test_verbatim_unique_hit_returns_correct_lines() -> None:
    out = reconcile_text_quote_selector(
        scene_text=_SCENE,
        exact="姜栀枝：你闭嘴。",
    )
    assert out == (7, 7)


def test_normalization_strips_punctuation_and_still_hits() -> None:
    # LLM 偶尔会丢标点 / 加空格；归一化后仍应命中
    out = reconcile_text_quote_selector(
        scene_text=_SCENE,
        exact="姜栀枝 你闭嘴",
    )
    assert out == (7, 7)


def test_action_line_with_triangle_marker() -> None:
    out = reconcile_text_quote_selector(
        scene_text=_SCENE,
        exact="△顾聿之摸住门把手，突然打开，冲进浴室",
    )
    assert out == (11, 11)


def test_quote_not_in_scene_returns_none() -> None:
    # LLM 自由摘要（"打脸 X" 这种）一定不在原文里 → 必须 reject
    out = reconcile_text_quote_selector(
        scene_text=_SCENE,
        exact="顾聿之冲进浴室抓陆斯言打脸姜栀枝的掩饰",
    )
    assert out is None


def test_short_quote_under_min_length_rejected() -> None:
    # < 4 字的 quote 在剧本里几乎一定多义 → fail-closed
    out = reconcile_text_quote_selector(
        scene_text=_SCENE,
        exact="嗯",
    )
    assert out is None


def test_multi_occurrence_without_context_rejected() -> None:
    multi = "测试句子 OK\n中间一行\n测试句子 OK"
    out = reconcile_text_quote_selector(scene_text=multi, exact="测试句子OK")
    assert out is None


def test_multi_occurrence_with_prefix_disambiguates() -> None:
    multi = "前半段甲\n测试句子\n中间一行\n前半段乙\n测试句子"
    out = reconcile_text_quote_selector(
        scene_text=multi,
        exact="测试句子",
        prefix="前半段乙",
    )
    assert out == (5, 5)


def test_multi_occurrence_with_suffix_disambiguates() -> None:
    multi = "测试句子\n后半段甲\n中间一行\n测试句子\n后半段乙"
    out = reconcile_text_quote_selector(
        scene_text=multi,
        exact="测试句子",
        suffix="后半段乙",
    )
    assert out == (4, 4)


def test_cross_line_quote_returns_range() -> None:
    cross = "第一行台词\n第二行台词\n第三行台词"
    out = reconcile_text_quote_selector(
        scene_text=cross,
        exact="第一行台词第二行台词",
    )
    assert out == (1, 2)


def test_empty_inputs_return_none() -> None:
    assert reconcile_text_quote_selector(scene_text="", exact="anything") is None
    assert reconcile_text_quote_selector(scene_text=_SCENE, exact="") is None
