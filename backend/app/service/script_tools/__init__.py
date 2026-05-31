"""ScriptLens 评分专用工具集合（utility 形态，不依赖 ReAct 框架）。

模块清单（rubric §4.3 工具签名）：
- `llm_caller`         评分专用 LLM 适配（OpenAI 主 + DashScope 兜底，强 JSON）
- `llm_cache`          结构化 LLM 缓存（input_hash 持久化）
- `scene_repo`         locate_scene / extract_quote / 多个 DB 查询 helper
- `script_ir`          场景重组 IR（line kind 分类）
- `chain_result`       LLM chain provenance 统一契约（ok/degraded/failed）
- `beat_chain`         三幕节拍生成（LLM + rule fallback）
- `coverage_chain`     速览卡（logline/synopsis/strengths/concerns/comparable）
- `character_graph_chain` 人物关系图（共现 + LLM enrich + 硬桥兜底）
- `character_pipeline` 角色实体识别 + 小传写作
- `motivation_chain`   动机评估
- `risk_terms`         广电六类红线关键词词表
- `risk_screener`      risk_screening
- `rewrite_chain`      rewrite_scene（D2-6）
- `reward_extractor`   高光事件抽取
- `compliance_scorer`  合规打分汇总

DEPRECATED（已在 PR3a 删除）：evaluation_chain / improvement_action_generator /
decision_aggregator / dimension_aggregator / genre_weights / percentile_tier /
v0_business_rule_baseline。
DEPRECATED（待 PR3b 删除）：tag_pipeline / bundle_extractor / plot_unit_segmenter /
extractor_common / rule_extractors / character_entity_resolver /
relationship_candidate_generator / tag_alignment_analyzer / match_config /
signal_catalog/**。
"""
