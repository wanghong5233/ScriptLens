"""ScriptLens 评分专用工具集合（utility 形态，不依赖 ReAct 框架）。

模块清单（rubric §4.3 工具签名）：
- `llm_caller`         评分专用 LLM 适配（OpenAI 主 + DashScope 兜底，强 JSON）
- `scene_repo`         locate_scene / extract_quote / 多个 DB 查询 helper
- `risk_terms`         广电六类红线关键词词表
- `reward_extractor`   extract_reward_events
- `motivation_chain`   score_motivation_chain
- `risk_screener`      risk_screening
- `dimension_scorer`   score_dimension（5 维通用入口）
- `rewrite_tool`       rewrite_scene（D2-6 实装，预留接口）
"""
