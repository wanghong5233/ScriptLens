from __future__ import annotations

from service.script_tools import rule_extractors as rules


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeConn(self._rows)


def test_infer_paid_break_position_ep_end() -> None:
    rows = []
    for idx in range(1, 11):
        if idx == 9:
            rows.append({"plot_unit_id": f"u{idx}", "idx": idx, "dim": "plot_hook", "value": "identity_reveal"})
        else:
            rows.append({"plot_unit_id": f"u{idx}", "idx": idx, "dim": None, "value": None})

    result = rules.infer_paid_break_position("sid-1", 1, engine=_FakeEngine(rows))
    assert result.position == "ep_end"
    assert result.anchor_idx == 9
    assert result.unit_count == 10


def test_persist_paid_break_position_writes_episode_tag(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        rules,
        "infer_paid_break_position",
        lambda script_id, episode_no, engine=None: rules.PaidBreakPositionResult(  # noqa: ARG005
            script_id=script_id,
            episode_no=episode_no,
            position="ep_mid",
            anchor_idx=5,
            unit_count=10,
            reason="hook_anchor",
        ),
    )
    monkeypatch.setattr(
        rules,
        "persist_episode_tags",
        lambda **kwargs: captured.update(kwargs),
    )
    result = rules.persist_paid_break_position(
        "sid-1",
        2,
        tag_set_ver="script",
        prompt_ver="rule:paid_break_position:a",
    )
    assert result.position == "ep_mid"
    assert captured["values_by_dim"]["paid_break_position"] == "ep_mid"
    assert captured["clear_existing"] is True
    assert captured["source"] == "rule"
