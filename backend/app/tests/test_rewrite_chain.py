import asyncio

from service.script_tools.llm_caller import LLMResponse
from service.script_tools.rewrite_chain import (
    _extract_scene_ids,
    execute_plan_step,
    propose_plan,
)


def test_extract_scene_ids_from_mixed_evidence() -> None:
    refs = [
        {"scene_id": "s1"},
        {"anchor": {"scene_id": "s2"}},
        {"id": "scene:s3"},
        "s4",
    ]
    assert _extract_scene_ids(refs) == ["s1", "s2", "s3", "s4"]


def test_propose_plan_from_action_candidates() -> None:
    scenes = [
        {
            "scene_id": "s1",
            "scene_label": "宴会冲突",
            "episode_no": 1,
            "scene_no": "1",
            "text": "原文",
            "matched_dimensions": ["story", "dialogue"],
            "dim_reasons": {"story": "主线因果断裂", "dialogue": "对白重复"},
            "expected_changes": ["压缩解释对白", "补转折触发条件"],
        }
    ]

    async def _run():
        plan = await propose_plan(
            script_id="script-1",
            dimensions=["story", "dialogue"],
            scenes=scenes,
        )
        assert plan.steps
        step = plan.steps[0]
        assert step.scene_id == "s1"
        assert "story" in step.target_dimensions
        assert "dialogue" in step.target_dimensions
        assert "压缩解释对白" in step.expected_changes

    asyncio.run(_run())


def test_execute_plan_step_uses_action_expected_changes(monkeypatch) -> None:
    from service.script_tools import rewrite_chain as chain

    fake_ctx = {
        "scene": {
            "id": "s1",
            "script_id": "script-1",
            "scene_label": "宴会冲突",
            "text": "原始场景文本",
        },
        "script_overview": "这是一个复仇短剧。",
        "characters_block": "- 主角（出场 10 场）",
        "prev_scenes_block": "- 第1集第1场：冲突起因",
        "next_scenes_block": "- 第1集第3场：后果升级",
    }

    monkeypatch.setattr(chain, "_load_rewrite_context", lambda *args, **kwargs: fake_ctx)
    monkeypatch.setattr(chain, "_load_scene_expected_changes", lambda *args, **kwargs: "压缩对白并补动作反馈")

    class _FakeCaller:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = kwargs
            self.prompts.append(prompt)
            return LLMResponse(
                raw="{}",
                parsed={"rewritten_text": "改写后文本", "rationale": "已按维度优化"},
                provider="openai",
                model="gpt-test",
                elapsed_ms=25,
            )

    fake = _FakeCaller()

    async def _run():
        result = await execute_plan_step(
            script_id="script-1",
            scene_id="s1",
            target_dimensions=["story", "dialogue"],
            expected_changes="",
            caller=fake,
        )
        assert result.scene_id == "s1"
        assert result.rewritten_text == "改写后文本"
        assert result.target_dimensions == ["story", "dialogue"]
        assert fake.prompts and "压缩对白并补动作反馈" in fake.prompts[0]

    asyncio.run(_run())
