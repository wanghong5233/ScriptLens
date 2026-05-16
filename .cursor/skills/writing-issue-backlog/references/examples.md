# Issue Backlog Examples · 反例 → 正例

## 反例：先入为主、补丁式方案、字段顺序倒置

```markdown
## ING-01 PDF 上传后分析为空

- 影响：用户上传 PDF 后生成报告没有内容
- 下一步：
  - Phase 1: PDF 统一转 TXT
  - Phase 2: 前端禁用扫描版 PDF
  - Phase 3: 后端接 OCR
```

问题：

1. 没有 `Repro`，他人无法复现
2. 没有 `Observed Evidence`，无法判断是解析失败、空白 PDF、编码问题还是前端传参问题
3. `Root Cause` 还没写，就跳到 3 个 Phase 的实现方案
4. "接 OCR"是启发式补丁，未经契约评审就被默认
5. 字段顺序错：先写"影响"再写"下一步"，跳过 Symptom/Repro/Evidence/Hypotheses/Root Cause

## 正例：类型声明 + 字段顺序 + 证据闭环

```markdown
## ING-01 PDF 上传后报告内容为空

- type: bug
- status: investigating
- priority: P1

### Symptom
上传某个 PDF 后点击生成报告，报告区显示空白或仅有通用占位文本。

### Repro
1. 打开本地前端 `http://127.0.0.1:3000`
2. 上传 `samples/bad_scan.pdf`
3. 点击「生成基础报告」
4. 观察前端报错、后端 `/files/extract` 响应和 `/analyze` 响应

环境：local dev, 2026-05-16 build。

### Observed Evidence
- `/files/extract` 响应：`extracted_text_length=0`
- 后端日志：`PdfReadError` 未出现
- 前端仍允许继续调用 `/analyze`

### Scope
仅影响扫描版或不可提取文本的 PDF；TXT 输入和可复制文本 PDF 正常。

### Impact
演示时用户会以为 Agent 分析能力失效；输入可信度受损。

### Hypotheses
- H1：PDF 本身是扫描图像，`pypdf` 无可提取文本
- H2：后端提取失败但被转换成空字符串
- H3：前端没有阻断空文本分析请求

### Open Questions
- Q1：同一 PDF 在本地阅读器中能否复制文本？
- Q2：`/files/extract` 是否应该对空文本返回 4xx？
- Q3：产品契约是否明确当前不支持 OCR？

### Root Cause
（待 Q1-Q3 完成后填写；当前禁止下结论）

### DoD
1. 空文本 PDF 有明确错误态，不生成伪报告
2. 前端文案说明当前支持可提取文本的 PDF/TXT
3. 样本覆盖 TXT、文本 PDF、扫描 PDF 三类路径

### Next Step
- Phase A（证据）：补 3 个输入样本和响应截图
- Phase B（决策）：确认是否将 OCR 列为非目标或后续 backlog
- Phase C：按决策落地，禁止在 Root Cause 前合并默认行为变更
```

## 反例：Improvement 类直接写实现

```markdown
## REP-04 分析报告改成流式输出

- 下一步：实现 SSE endpoint `/api/scripts/{id}/analyze/stream`
```

问题：没说明当前行为、为什么需要改、什么条件触发改造，直接跳实现。

## 正例：Improvement 类带 Trigger Condition

```markdown
## REP-04 报告生成等待时间偏长

- type: improvement
- status: triaging
- priority: P3

### Current Behavior
报告生成走一次性 HTTP 响应，前端等待完成后展示完整报告。

### Limitation
短样本可接受；长剧本或真实 LLM 调用时用户无法看到阶段进度。

### Trigger Condition
满足以下任一即升级到 `planned`：
- 分析 p95 ≥ 45s
- 用户反馈「不知道是否卡住」≥ 3 次 / 月
- 真实 demo 样本超过 30k 字成为常态

### Options Considered
- SSE：服务器推送阶段进度；前端 listener
- 轮询：兼容性好但延迟仍在
- 任务队列：更稳但超出当前考核 MVP

### DoD
1. 触发条件达成时启动方案评审
2. 实现后用户可看到 ingest/segment/analyze/report 阶段
3. 旧同步接口保留为 fallback

### Next Step
等触发条件达成；当前仅收集运行时间和用户反馈。
```
