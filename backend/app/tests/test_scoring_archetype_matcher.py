"""scoring v4 archetype_matcher 测试。"""

from __future__ import annotations

from service.scoring.archetype_matcher import (
    match_character_archetype,
    match_genre_archetype,
)


def test_match_genre_zhanshen_pattern() -> None:
    text = """主角是退役兵王，归来时被人瞧不起。后来身份揭露，扫地出门的他重出江湖。
战神归来，都市最强。"""
    matches = match_genre_archetype(text)
    assert matches, "战神文本应命中至少 1 个原型"
    assert matches[0].archetype.id == "zhanshen_guilai"
    assert matches[0].score > 0


def test_match_genre_chongsheng_pattern() -> None:
    text = "她重生回到三年前，记得前世被欺辱的一切。这一世，她要复仇。"
    matches = match_genre_archetype(text)
    assert matches
    assert matches[0].archetype.id == "chongsheng_fuchou"


def test_match_genre_chuanyue_xitong_pattern() -> None:
    text = "她穿越到了书里，绑定了系统。宿主，请完成第一个任务。"
    matches = match_genre_archetype(text)
    assert matches
    assert matches[0].archetype.id == "chuanyue_xitong"


def test_match_genre_empty_text_no_match() -> None:
    matches = match_genre_archetype("")
    assert matches == []


def test_match_genre_irrelevant_text_no_match() -> None:
    matches = match_genre_archetype("今天天气很好，我去公园散步。")
    # 不应有原型 match（或 score 极低）
    if matches:
        assert matches[0].score < 0.2


def test_match_character_archetype_bazong() -> None:
    inputs = ["陆总 霸道总裁 京圈太子爷", "苏婉 善良 单纯 委屈"]
    results = match_character_archetype(inputs)
    assert len(results) >= 1
    # 第一个应命中 bazong / 第二个应命中灰姑娘
    matched_archetype_ids = {m.archetype.id for _, m in results}
    assert "bazong" in matched_archetype_ids


def test_match_character_archetype_no_match_filtered() -> None:
    results = match_character_archetype(["完全不沾边的随便人名"])
    assert results == []
