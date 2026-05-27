"""ScriptLens 评分专用工具集合（utility 形态，不依赖 ReAct 框架）。

模块清单（rubric §4.3 工具签名）：
- `llm_caller`         评分专用 LLM 适配（OpenAI 主 + DashScope 兜底，强 JSON）
- `llm_cache`          结构化 LLM 缓存（input_hash 持久化）
- `scene_repo`         locate_scene / extract_quote / 多个 DB 查询 helper
- `script_ir`          场景重组 IR（line kind 分类）
- `plot_unit_segmenter`  情节单元切分（候选边界 + LLM 复核）
- `character_entity_resolver`  角色实体归一（alias 聚合）
- `bundle_extractor`           schema-driven bundle 抽取器（v0/v1/v2 共用）
- `tag_pipeline`               schema-driven 统一标签流水线入口
- `v1_extractor_common`        v1 character/relationship/episode 上下文加载
- `relationship_candidate_generator` 关系候选生成（共现阈值 + top-K）
- `v0_business_rule_baseline`  v0 business_* 规则对照基线
- `risk_terms`         广电六类红线关键词词表
- `reward_extractor`   extract_reward_events
- `motivation_chain`   score_motivation_chain
- `risk_screener`      risk_screening
- `dimension_scorer`   score_dimension（5 维通用入口）
- `rewrite_tool`       rewrite_scene（D2-6 实装，预留接口）
"""
