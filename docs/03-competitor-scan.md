# ScriptLens 竞品调研

## 1. 调研目的

本调研只服务 `docs/source/task.md` 的交付目标:设计并实现一个能帮助用户高效理解长剧本的 Agent。

调研不追求覆盖所有泛用 AI 产品,重点看两类对象:

- 垂直剧本/影视内容分析产品。
- 长文本剧本交互产品。

核心问题:

- 行业里"剧本分析"通常输出什么?
- 它们如何区别于普通摘要?
- 哪些能力可以直接启发 ScriptLens?
- 哪些能力不适合 10 天考核期实现?

## 2. 行业共识

垂直剧本 AI 产品的共同点是:它们都不把自己定位为"摘要器",而是定位为 script coverage、story intelligence、market analytics 或 development notes。

主流输出由三层组成:

- **Coverage Report**:logline、synopsis、人物、结构、主题、市场潜力、最终推荐。
- **Story Analytics**:节奏、情绪、类型、人物关系、场景强弱、数据图表。
- **Script Chat / Development Notes**:围绕剧本追问、定位场景、给具体改稿建议。

这说明 ScriptLens 的正确方向是"决策支持 + 证据定位 + 可交互改进",不是"剧情压缩"。

## 3. Prescene

官网:

- https://www.prescene.ai/
- https://www.prescene.ai/features

定位:

- 面向 film/TV 的 story intelligence 平台。
- 从 first draft 到 green light,服务开发、分析和 pitch。

关键能力:

- 3 分钟生成分析。
- studio-quality coverage。
- character breakdowns。
- actionable feedback。
- Script Chat。
- scene-level citations。
- 导出报告、团队分享。

对 ScriptLens 的启发:

- "Chat with your script" 是题目中"可以与用户交互"的垂直版本。
- "scene-level citations" 证明原文依据不是附加功能,而是信任核心。
- "coverage + chat" 的组合比单一报告更接近 Agent。
- 前端必须能把报告、原文、证据和聊天连接起来。

不照抄的点:

- Prescene 面向标准 screenplay 和电影电视生产流程,ScriptLens 要兼容短剧、网文、未整理文本。
- 不在 MVP 中做预算、拍摄计划、版权保护等生产管理功能。

## 4. AIScriptReader

官网:

- https://aiscriptreader.com/ai-script-reader
- https://nextjs.aiscriptreader.com/screenplay-analysis
- https://nextjs.aiscriptreader.com/screenplay-coverage

定位:

- 用 AI 生成 professional script coverage。
- 强调像 studio readers 一样输出结构化报告。

关键能力:

- 11-section coverage report。
- logline、story overview、full synopsis。
- character breakdown、character arc analysis。
- plot & structure analysis。
- themes and motifs。
- strengths、weaknesses、recommendations。
- market potential assessment。
- development notes。
- premise、structure、characters、dialogue、overall impression 等量化评分。

对 ScriptLens 的启发:

- 输出结构要职业化,不能像聊天机器人随口总结。
- 评分维度要围绕故事判断,例如结构、人物、节奏、市场潜力。
- "development recommendations" 可以对应低评级改写能力。
- "act-by-act synopsis" 可以改造成中文短剧/网文的"剧情节点时间线"。

不照抄的点:

- 不强依赖三幕剧结构,因为样本可能是网文、短剧、未整理文本。
- 不只服务编剧改稿,还要服务选品、投放、审核等岗位。

## 5. StoryFit

官网:

- https://storyfit.com/
- https://storyfit.com/products/analytics/
- https://storyfit.com/what-can-a-storyfit-content-insights-tell-you/

定位:

- Entertainment analytics。
- 帮助创作者、制片、营销、销售理解故事和受众。

关键能力:

- Audience Insights:故事会打动谁。
- Narrative Insights:故事的 superpowers、risks、opportunities。
- Character Insights:人物是否有吸引力,是否能承载故事。
- 主题、动作、对白、情绪、地点、情节特征分析。
- 受众反应、年龄分级、对白/动作比例、主要人物数量。
- studio preference 可按机构偏好定制。

对 ScriptLens 的启发:

- "superpowers / risks / opportunities" 很适合转化为 ScriptLens 的"核心价值 / 问题风险 / 推进建议"。
- 角色、情绪、对白/动作比例等可以启发数据分析能力。
- 不同岗位视角可以借鉴其 research / creative / marketing / executive 分类。

不照抄的点:

- StoryFit 有大量专有模型和历史数据,ScriptLens 不应伪装成真实市场预测系统。
- D1-D10 只做基于文本证据的相对评分和解释,不做真实观众预测。

## 6. ScriptBook

公开信息:

- https://pr.linkedin.com/company/scriptbook
- 第三方介绍显示其强调 screenplay analysis、commercial/critical success prediction、dashboard、color-coded score。

定位:

- 偏商业预测和 greenlight 决策。
- 强调商业成功、观众和市场表现预测。

关键能力:

- 故事线、人物发展、目标受众、市场潜力。
- 票房、观众评分、类型适配等预测。
- 可视化 dashboard 和颜色评分。

对 ScriptLens 的启发:

- 选品/推进视角需要明确的"推荐 / 谨慎 / 不推荐"。
- 评分要可视化,让用户快速识别强弱项。
- 商业潜力可以做成"文本内证据驱动的潜力判断",而不是绝对市场预测。

不照抄的点:

- 不做票房或收益预测。
- 不声称预测准确率,避免超出当前数据能力。

## 7. Largo.ai

官网:

- https://home.largo.ai/accelerate-your-film-development-and-financing-with-ai-insights/
- https://home.largo.io/largo-content-insights/

定位:

- 面向电影开发、融资、发行的 AI insights。
- 从 screenplay 或 video 分析内容与受众。

关键能力:

- genre analysis。
- emotion analysis。
- character relationships。
- demographics prediction。
- comparables。
- casting suggestions。
- streaming / box office forecasts。
- simulated audiences。

对 ScriptLens 的启发:

- 情绪曲线、类型定位、人物关系图适合前端展示。
- "scene by scene" 的动态分析适合做剧情节点和节奏热力图。
- 可把"潜在表现"转成"传播点、爽点密度、前半段吸引力"。

不照抄的点:

- 不做 casting、融资、发行预测。
- 不做模拟观众数字孪生。
- 不做基于外部影片库的 comparables,除非后续有数据源。

## 8. Scriptwood

官网:

- https://www.scriptwood.com/
- https://scriptwood.com/

定位:

- AI script coverage & analysis tool。
- 服务 writers、studios、agents、producers。

关键能力:

- plot structure analysis。
- character development。
- market potential。
- data charts。
- technical elements: tone、world building、logic、themes、freshness。
- development notes。
- industry comparison。
- overall rating。

对 ScriptLens 的启发:

- "technical elements" 可以补足题目里的"角色动机是否成立、是否存在理解障碍"。
- data charts 说明前端不只是文本,需要图形化表达。
- 可借鉴"strengths / weaknesses / next steps" 作为报告固定区块。

不照抄的点:

- 不做 industry comparison,除非只是轻量类型参照。
- 不把报告做成英文 screenplay coverage,而要适配中文短剧/网文场景。

## 9. ScriptReader.ai / AI Script Coverage Pro / OnDesk

官网:

- https://scriptreader.ai/
- https://aiscriptcoveragepro.com/
- https://www.itsondesk.com/script-coverage

补充观察:

- ScriptReader.ai 强调 scene-by-scene analysis、weakest scene、skill breakdown、emotional analysis、scene improvement with AI。
- AI Script Coverage Pro 强调 quick/professional/market analysis、50+ industry criteria、dashboard、track improvements。
- OnDesk 明确说明 coverage report 包括 logline、full synopsis、character analysis、structural breakdown、market assessment、final recommendation,并强调让决策者用 5 分钟理解剧本。

对 ScriptLens 的启发:

- "weakest scene" 可以转化为"最需要改写的段落"。
- "track improvements" 可以转化为"改写前后评分对比"。
- "5 分钟理解剧本" 与题目中的"短时间内准确理解"高度一致。

## 10. 对 ScriptLens 的产品结论

ScriptLens 应该做成:

- 中文短剧/网文/剧本场景的 coverage agent。
- 结构化报告 + 原文证据 + 多轮追问 + 低分改写。
- 面向选品、策划、投放、审核等多视角决策。
- 前端以理解效率为核心,不是普通聊天界面。

必须具备的差异化:

- 兼容未整理中文长文本。
- 输出围绕"值不值得继续看"组织。
- 每个关键判断能点回原文。
- 评分不装成真实市场预测,而是说明文本内依据。
- 改写围绕具体低分维度,不是泛泛润色。
- 用户反馈能沉淀成临时 skill 或分析视角。

## 11. 不做清单

为了 10 天高质量交付,明确不做:

- 不做真实票房预测。
- 不做真实受众画像预测。
- 不做 casting 和预算计划。
- 不做外部电影库 comparables。
- 不做专业版权清查。
- 不做多用户团队协作。
- 不做复杂训练或后训练。

这些不是题目要求的核心,会拖累交付。

## 12. 设计落点

竞品调研最终转化为 ScriptLens 的 6 个设计落点:

1. **Decision Card**:30 秒给出推荐/谨慎/不推荐。
2. **Coverage Report**:主线、人物、冲突、看点、节奏、风险。
3. **Scorecard**:质量、结构、人物、节奏、爽点、潜力、风险评分。
4. **Evidence Viewer**:点击结论定位原文。
5. **Script Chat**:围绕剧本追问并引用证据。
6. **Development Agent**:低分项建议和定向改写。

这 6 个落点覆盖基础要求和全部加分项。
