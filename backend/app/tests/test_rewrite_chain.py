import asyncio

import pytest

from service.script_tools.llm_caller import LLMResponse, ScoreLLMError
from service.script_tools.rewrite_chain import (
    SceneBrief,
    execute_plan_step,
    propose_plan,
)


def test_propose_plan_llm_first_with_improvement_brief(monkeypatch) -> None:
    """improvement 路径：传 improvement_brief 时 LLM 主导选场。

    覆盖回归点：propose_plan 在 dimension_keys / improvement_brief 任一非空时
    不再抛 ValueError；LLM 返回的 scene_id 经过存在性校验后才进入 plan。
    """

    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-aaa",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "早餐桌冲突",
            "characters": "妈/爸/姐/弟/我",
            "digest": "5 角色同时在场，争吵打断。",
        },
        {
            "scene_id": "scene-bbb",
            "episode_no": 1,
            "scene_no": "2",
            "scene_label": "走廊独白",
            "characters": "我",
            "digest": "独白推动情绪。",
        },
    ]

    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "一段都市复仇短剧概要。")
    monkeypatch.setattr(
        chain,
        "_load_latest_verdict_snapshot",
        lambda **_k: {
            "investment_score": 6.2,
            "verdict_label": "PASS_PROVISIONAL",
            "verdict_reason": "钩子在线但人物动机偏弱。",
            "top_improvements": [
                {"title": "压缩同框人数", "dimension_label": "可生成力"},
            ],
        },
    )
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    seen_prompt: dict[str, str] = {}

    class _FakeCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = kwargs
            seen_prompt["body"] = prompt
            return LLMResponse(
                raw="{}",
                parsed={
                    "overall_summary": "把同框 5 角色场拆成 2 角色焦点场，降低制作复杂度。",
                    "steps": [
                        {
                            "scene_id": "scene-aaa",
                            "target_dimensions": ["producibility"],
                            "rationale": "5 角色同框，AI 视频多角色一致性短板。",
                            "expected_changes": "拆成两组对话，主线只保留 2 个角色出场。",
                        }
                    ],
                },
                provider="openai",
                model="gpt-test",
                elapsed_ms=42,
            )

    async def _run():
        plan = await propose_plan(
            script_id="script-9",
            dimension_keys=["producibility"],
            improvement_brief={
                "title": "压缩单场同时在场角色数",
                "rationale": "避免单场 5+ 角色同时在场（AI 视频多角色一致性短板）。",
                "dimension_key": "producibility",
                "dimension_label": "可生成力",
                "signal_key": "concurrent_characters",
                "signal_label": "单场同框人数可控",
                "evidence_ref_ids": [],
            },
            caller=_FakeCaller(),
        )
        assert plan.steps and plan.steps[0].scene_id == "scene-aaa"
        assert "producibility" in plan.steps[0].target_dimensions
        assert "压缩同框" in (plan.overall_summary or "") or "降低制作" in (
            plan.overall_summary or ""
        )
        body = seen_prompt.get("body", "")
        assert "压缩单场同时在场角色数" in body
        assert "scene-aaa" in body
        assert "producibility" in body or "可生成力" in body

    asyncio.run(_run())


def test_propose_plan_llm_coerces_scalar_target_dimensions(monkeypatch) -> None:
    """LLM 把 target_dimensions 吐成 scalar 字符串时，schema 应自动转 list。

    回归点：line 'Input should be a valid list ... input_value=\\'producibility\\''
    曾在生产链路阻断 propose_plan 全部 step。
    """

    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-xyz",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "母女对峙",
            "characters": "妈/我",
            "digest": "母女对峙",
        }
    ]
    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧概要。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    class _FakeCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            return LLMResponse(
                raw="{}",
                parsed={
                    "overall_summary": "前 3 场补全钩子链。",
                    "steps": [
                        {
                            "scene_id": "scene-xyz",
                            "target_dimensions": "producibility",  # scalar，不是 list
                            "rationale": "5 角色同框，建议拆。",
                            "expected_changes": "拆成两组对话。",
                        }
                    ],
                },
                provider="openai",
                model="gpt-test",
                elapsed_ms=15,
            )

    async def _run():
        plan = await propose_plan(
            script_id="s-1",
            improvement_brief={
                "title": "压缩同框人数",
                "rationale": "AI 视频多角色一致性短板。",
                "dimension_key": "producibility",
                "dimension_label": "可生成力",
            },
            caller=_FakeCaller(),
        )
        assert plan.steps and plan.steps[0].target_dimensions == ["producibility"]

    asyncio.run(_run())


def test_propose_plan_llm_first_rejects_hallucinated_scene_ids(monkeypatch) -> None:
    """LLM 返回不存在的 scene_id 时整 plan 视为失败（避免幻觉 plan 流到前端）。"""

    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-real",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "厨房早餐",
            "characters": "妈/我",
            "digest": "母女早餐对话。",
        }
    ]
    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧概要。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    class _FakeCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            return LLMResponse(
                raw="{}",
                parsed={
                    "overall_summary": "幻觉 plan。",
                    "steps": [
                        {
                            "scene_id": "scene-FAKE",  # 不在 catalog 里
                            "target_dimensions": ["hook"],
                            "rationale": "瞎想。",
                            "expected_changes": "也瞎想。",
                        }
                    ],
                },
                provider="openai",
                model="gpt-test",
                elapsed_ms=20,
            )

    async def _run():
        with pytest.raises(ScoreLLMError, match="no valid steps"):
            await propose_plan(
                script_id="script-9",
                improvement_brief={
                    "title": "x",
                    "rationale": "y",
                    "dimension_key": "hook",
                    "dimension_label": "抓人力",
                },
                caller=_FakeCaller(),
            )

    asyncio.run(_run())


def test_propose_plan_no_driver_raises_value_error() -> None:
    """dimension_keys / improvement_brief / diagnostic_brief 全空 → 立即报错。"""

    async def _run():
        with pytest.raises(ValueError, match="propose_plan"):
            await propose_plan(script_id="s1")

    asyncio.run(_run())


def test_execute_plan_step_invokes_llm_with_investment_dims(monkeypatch) -> None:
    """execute_plan_step 只接受投资决策五维键，prompt 内能拿到 expected_changes。"""

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
            target_dimensions=["hook", "payoff"],
            expected_changes="开篇 3 秒抛出反向悬念，结尾兑现承诺。",
            caller=fake,
        )
        assert result.scene_id == "s1"
        assert result.rewritten_text == "改写后文本"
        assert result.target_dimensions == ["hook", "payoff"]
        prompt = fake.prompts[0]
        assert "开篇 3 秒抛出反向悬念" in prompt
        assert "hook" in prompt and "payoff" in prompt

    asyncio.run(_run())


def test_execute_plan_step_rejects_legacy_dimensions() -> None:
    """老 v3 维度键（story / character / ...）必须被拒收，强制走五维。"""

    async def _run():
        with pytest.raises(ValueError, match="target_dimensions"):
            await execute_plan_step(
                script_id="script-1",
                scene_id="s1",
                target_dimensions=["story", "dialogue"],
                expected_changes="x",
            )

    asyncio.run(_run())


def test_plan_prompt_includes_dimension_guidance_and_first_principles(
    monkeypatch,
) -> None:
    """C 档：plan prompt 必须按 dimension_key 注入维度专属方法论 md + 第一性原理。

    回归点：dimension_key=producibility 时，prompt 应当包含纠正"减角色"误读的
    维度方法论文案（来自 prompts/script_studio/plan/by_dimension/producibility.zh.md）
    以及禁止删主角的硬约束。
    """
    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-aaa",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "群戏",
            "characters_raw": ["主A", "配B", "龙套C"],
            "characters_by_role": {
                "protagonist": ["主A"],
                "antagonist": [],
                "support": ["配B"],
                "minor": ["龙套C"],
            },
            "brief_json": None,
            "digest": "群戏场。",
        }
    ]
    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧概要。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    captured: dict[str, str] = {}
    call_count = {"n": 0}

    planner_response = LLMResponse(
        raw="{}",
        parsed={
            "overall_summary": "压缩 producibility 短板",
            "steps": [
                {
                    "scene_id": "scene-aaa",
                    "target_dimensions": ["producibility"],
                    "rationale": "本场配角配B和龙套C同框",
                    "expected_changes": "合并配B/龙套C",
                }
            ],
        },
        provider="openai",
        model="gpt-test",
        elapsed_ms=42,
    )
    critic_response = LLMResponse(
        raw="{}",
        parsed={
            "overall_summary": "压缩 producibility 短板",
            "steps_kept": [
                {
                    "scene_id": "scene-aaa",
                    "target_dimensions": ["producibility"],
                    "rationale": "本场配角配B和龙套C同框",
                    "expected_changes": "合并配B/龙套C",
                    "critic_action": "kept",
                }
            ],
            "steps_dropped": [],
        },
        provider="openai",
        model="gpt-test",
        elapsed_ms=42,
    )

    class _StagedCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = kwargs
            call_count["n"] += 1
            if call_count["n"] == 1:
                captured["planner_prompt"] = prompt
                return planner_response
            captured["critic_prompt"] = prompt
            return critic_response

    async def _run():
        plan = await propose_plan(
            script_id="script-fp",
            improvement_brief={
                "title": "压缩 producibility 短板",
                "rationale": "群戏密度高",
                "dimension_key": "producibility",
                "dimension_label": "可生成力",
                "signal_key": "group_density",
                "evidence_ref_ids": [],
            },
            caller=_StagedCaller(),
        )
        assert plan.steps and plan.steps[0].scene_id == "scene-aaa"
        body = captured["planner_prompt"]
        # 第一性原理硬约束
        assert "短剧改写第一性原理" in body
        assert "主角必须复用" in body or "主角的高频出现" in body
        # producibility 维度专属方法论（md 加载）
        assert "## 维度：producibility" in body
        assert "Bad rationale" in body  # 维度 md 含 few-shot
        # 输出契约里禁止主角删除规则
        assert "主角 / 反派禁止删除" in body
        # critic 阶段也调了
        assert "critic_prompt" in captured
        assert "你是中文 AI 漫剧" in captured["critic_prompt"]
        assert "scene-aaa" in captured["critic_prompt"]

    asyncio.run(_run())


def test_critique_plan_fallback_when_critic_returns_planner_shape(monkeypatch) -> None:
    """C 档：critic LLM 跑偏返回 planner 格式时，propose_plan 必须 fallback 到
    planner 原始输出，而不是误把所有 step 当作 dropped。"""
    from service.script_tools import rewrite_chain as chain

    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(
        chain,
        "_load_scene_catalog",
        lambda **_k: [
            {
                "scene_id": "scene-x",
                "episode_no": 1,
                "scene_no": "1",
                "scene_label": "场",
                "characters_raw": ["主A"],
                "characters_by_role": {
                    "protagonist": ["主A"],
                    "antagonist": [],
                    "support": [],
                    "minor": [],
                },
                "brief_json": None,
                "digest": "x",
            }
        ],
    )

    # planner 和 critic 都返回 planner-shape JSON — critic 必须 fallback
    same_response = LLMResponse(
        raw="{}",
        parsed={
            "overall_summary": "planner 输出",
            "steps": [
                {
                    "scene_id": "scene-x",
                    "target_dimensions": ["hook"],
                    "rationale": "主A 开场没冲突",
                    "expected_changes": "把第 1 行台词改为强冲突",
                }
            ],
        },
        provider="openai",
        model="gpt-test",
        elapsed_ms=10,
    )

    class _AlwaysPlannerShape:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            return same_response

    async def _run():
        plan = await propose_plan(
            script_id="script-fb",
            dimension_keys=["hook"],
            caller=_AlwaysPlannerShape(),
        )
        assert plan.steps and plan.steps[0].scene_id == "scene-x"
        assert "主A 开场没冲突" in plan.steps[0].rationale

    asyncio.run(_run())


# ============================================================
# C1c: scene_brief 生成器单测
# ============================================================


def test_scene_brief_pydantic_coerces_loose_llm_outputs() -> None:
    """SceneBrief schema 必须对 LLM 常见格式偏差做容错：

    - scene_function/group_density 缺失或写成"中"/"高"等中文，回退到合法枚举
    - protagonist_actions 吐成 scalar string，自动转单元素 list
    - removable_characters 吐 None，转空 list
    """
    b = SceneBrief.model_validate(
        {
            "conflict": "妈拒绝我相亲",
            "scene_function": None,
            "group_density": "高",
            "protagonist_actions": "我 摔了门",
            "supporting_actions": [],
            "removable_characters": None,
        }
    )
    assert b.scene_function == "过渡"
    assert b.group_density == "high"
    assert b.protagonist_actions == ["我 摔了门"]
    assert b.supporting_actions == []
    assert b.removable_characters == []


def test_generate_single_brief_success_returns_brief(monkeypatch) -> None:
    """_generate_single_brief：LLM 返回合法 SceneBrief 时直接落 pydantic 模型。"""
    from service.script_tools import rewrite_chain as chain

    captured: dict[str, str] = {}

    class _FakeCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = kwargs
            captured["prompt"] = prompt
            return LLMResponse(
                raw="{}",
                parsed=SceneBrief(
                    conflict="妈拒绝我相亲",
                    scene_function="推进主线",
                    protagonist_actions=["我 反驳妈"],
                    supporting_actions=["妈 摔筷子"],
                    removable_characters=[],
                    group_density="low",
                ),
                provider="openai",
                model="gpt-test",
                elapsed_ms=20,
            )

    async def _run():
        brief = await chain._generate_single_brief(
            scene_row={
                "id": "scene-1",
                "episode_no": 1,
                "scene_no": "1",
                "scene_label": "厨房早餐",
                "characters": ["我", "妈"],
                "text": "妈把饭碗摔了。\n我：我不去相亲。",
            },
            role_map={"我": "protagonist", "妈": "support"},
            caller=_FakeCaller(),
        )
        assert isinstance(brief, SceneBrief)
        assert brief.conflict == "妈拒绝我相亲"
        assert brief.scene_function == "推进主线"
        # prompt 应当带上角色分桶和原文
        body = captured["prompt"]
        assert "我" in body and "妈" in body
        assert "厨房早餐" in body
        assert "我不去相亲" in body

    asyncio.run(_run())


def test_generate_single_brief_llm_error_returns_none(monkeypatch) -> None:
    """LLM 抛 ScoreLLMError / Timeout / 任意异常时，函数静默返回 None
    （best-effort 设计，不破坏主流程）。
    """
    from service.script_tools import rewrite_chain as chain

    class _FailingCaller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            raise ScoreLLMError("provider-down")

    async def _run():
        brief = await chain._generate_single_brief(
            scene_row={
                "id": "scene-1",
                "episode_no": 1,
                "scene_no": "1",
                "scene_label": "x",
                "characters": ["我"],
                "text": "原文。",
            },
            role_map={},
            caller=_FailingCaller(),
        )
        assert brief is None

    asyncio.run(_run())


def test_propose_plan_invokes_ensure_scene_briefs_for_priority(monkeypatch) -> None:
    """C1c：propose_plan 在解析 improvement_brief 拿到 priority_scene_ids 后，
    必须调 _ensure_scene_briefs(priority_ids) 做预热（best-effort，失败也不影响主路径）。
    """
    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-prio",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "厨房早餐",
            "characters_raw": ["我", "妈"],
            "characters_by_role": {
                "protagonist": ["我"],
                "antagonist": [],
                "support": ["妈"],
                "minor": [],
            },
            "brief_json": None,
            "digest": "x",
        }
    ]
    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    ensure_calls: list[list[str]] = []

    async def _fake_ensure(scene_ids, **kwargs):  # noqa: ANN001
        _ = kwargs
        ensure_calls.append(list(scene_ids))
        return 0  # 0 个新写入 — 不触发 catalog 重新加载

    monkeypatch.setattr(chain, "_ensure_scene_briefs", _fake_ensure)

    class _Caller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            return LLMResponse(
                raw="{}",
                parsed={
                    "overall_summary": "x",
                    "steps_kept": [
                        {
                            "scene_id": "scene-prio",
                            "target_dimensions": ["hook"],
                            "rationale": "本场开场没有强钩子",
                            "expected_changes": "增加 3 秒强冲突镜头",
                            "critic_action": "kept",
                        }
                    ],
                    "steps_dropped": [],
                    "steps": [
                        {
                            "scene_id": "scene-prio",
                            "target_dimensions": ["hook"],
                            "rationale": "本场开场没有强钩子",
                            "expected_changes": "增加 3 秒强冲突镜头",
                        }
                    ],
                },
                provider="openai",
                model="gpt-test",
                elapsed_ms=15,
            )

    async def _run():
        plan = await propose_plan(
            script_id="script-bf",
            improvement_brief={
                "title": "hook 短板",
                "rationale": "x",
                "dimension_key": "hook",
                "dimension_label": "抓人力",
                "evidence_ref_ids": ["scene-prio"],
            },
            caller=_Caller(),
        )
        assert plan.steps
        # ensure 必须被调用过一次，传入的就是 evidence_ref_ids
        assert ensure_calls, "_ensure_scene_briefs was not invoked"
        assert "scene-prio" in ensure_calls[0]

    asyncio.run(_run())


def test_propose_plan_continues_when_ensure_briefs_fails(monkeypatch) -> None:
    """C1c best-effort：_ensure_scene_briefs 抛异常时 propose_plan 必须继续走完
    plan/critic，不能把 brief 故障传染到主路径。
    """
    from service.script_tools import rewrite_chain as chain

    fake_catalog = [
        {
            "scene_id": "scene-x",
            "episode_no": 1,
            "scene_no": "1",
            "scene_label": "x",
            "characters_raw": ["我"],
            "characters_by_role": {
                "protagonist": ["我"],
                "antagonist": [],
                "support": [],
                "minor": [],
            },
            "brief_json": None,
            "digest": "x",
        }
    ]
    monkeypatch.setattr(chain, "_load_script_overview", lambda *_a, **_k: "短剧。")
    monkeypatch.setattr(chain, "_load_latest_verdict_snapshot", lambda **_k: None)
    monkeypatch.setattr(chain, "_load_scene_catalog", lambda **_k: fake_catalog)

    async def _boom(*_a, **_k):
        raise RuntimeError("brief storage offline")

    monkeypatch.setattr(chain, "_ensure_scene_briefs", _boom)

    class _Caller:
        async def call_json(self, prompt: str, **kwargs):  # noqa: ANN003
            _ = prompt, kwargs
            return LLMResponse(
                raw="{}",
                parsed={
                    "overall_summary": "fallback ok",
                    "steps_kept": [
                        {
                            "scene_id": "scene-x",
                            "target_dimensions": ["hook"],
                            "rationale": "本场开场没有强钩子",
                            "expected_changes": "增加 3 秒强冲突镜头",
                            "critic_action": "kept",
                        }
                    ],
                    "steps_dropped": [],
                    "steps": [
                        {
                            "scene_id": "scene-x",
                            "target_dimensions": ["hook"],
                            "rationale": "本场开场没有强钩子",
                            "expected_changes": "增加 3 秒强冲突镜头",
                        }
                    ],
                },
                provider="openai",
                model="gpt-test",
                elapsed_ms=10,
            )

    async def _run():
        plan = await propose_plan(
            script_id="script-ge",
            improvement_brief={
                "title": "hook 短板",
                "rationale": "x",
                "dimension_key": "hook",
                "dimension_label": "抓人力",
                "evidence_ref_ids": ["scene-x"],
            },
            caller=_Caller(),
        )
        assert plan.steps  # plan 必须照常返回
        assert plan.steps[0].scene_id == "scene-x"

    asyncio.run(_run())
