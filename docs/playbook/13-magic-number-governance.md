# Magic Number 治理规范

## 1. 目标

统一治理散落的硬编码数字，降低“看不懂为什么是这个值”的维护成本，并建立可持续约束，避免回潮。

## 2. 定义（什么是要治理的魔法数字）

以下数字默认禁止内联，需抽取为具名常量并写明依据：

- 业务阈值：例如大小限制、分页上限、评分边界、重试次数。
- 协议语义：例如 HTTP 状态码、自定义错误码、超时时间。
- 算法参数：例如权重、阈值、衰减系数、采样率。
- UI/导出关键尺寸：例如分页尺寸、渲染宽度、延时。

以下数字可内联（白名单）：

- `-2, -1, 0, 1, 2`（循环、索引、常见哨兵）
- 枚举字面量、类型声明中的数字（由 lint 规则忽略）

## 3. 依据写法（必须回答“为什么是这个数字”）

每个新增常量应至少满足以下之一：

1. **标准来源**：如 RFC / IANA / 官方 SDK 约束；
2. **产品约束**：如 PRD / 需求文档明确值；
3. **工程经验值**：有明确权衡（性能、延迟、可读性），并可调；
4. **兼容性约束**：与已有数据、协议或前后端契约对齐。

推荐写法：

```ts
// PDF 渲染等待 200ms：等待字体与 SVG 替换完成，减少空白页概率
export const PDF_RENDER_SETTLE_DELAY_MS = 200
```

```py
# 500: HTTP internal server error, 与 FastAPI status 常量对齐
DEFAULT_ERROR_STATUS_CODE = status.HTTP_500_INTERNAL_SERVER_ERROR
```

## 4. 本仓已落地机制

### 前端

- 新增 `frontend/eslint.config.mjs`，启用 `@typescript-eslint/no-magic-numbers`（warning）。
- 新增 `npm run lint:magic-numbers`，可用于将该规则按需提升到 error 检查。
- 新增 `frontend/src/constants/numbers.ts` 统一承载跨模块数字常量。

### 后端

- 新增 `backend/pyproject.toml`，启用 Ruff（含 `PLR2004`）。
- 说明：当前环境未安装 `ruff` 可执行文件；在 CI/开发机安装后即可生效。
- 对“配置中心型文件”（如 `app/core/config.py`）做了按文件豁免，避免对大量配置默认值误报。

## 4.1 当前遗留与例外

- `backend/app/database/knowledgebase_operations.py` 当前仍保留 `461`（业务历史约定，前端已依赖）。
- 该值不是 IANA 标准状态码，后续建议迁移为 `404` + 结构化业务错误码（如 `KB_NOT_FOUND`），避免协议歧义。

## 5. 渐进式根治方案（推荐）

1. **第一阶段（已可执行）**  
   规则告警 + 新增代码必须抽常量。

2. **第二阶段（按目录收敛）**  
   从高变更目录开始（例如 `frontend/src/api`、`frontend/src/utils`），逐批消除 warning。

3. **第三阶段（CI 闸门）**  
   在重点目录把 magic-number 规则提升为 error；全量稳定后再扩展到全仓。

## 6. 代码评审准入

PR 审查至少检查：

- 新增数字是否具名常量化；
- 常量名是否体现单位/语义（`_MS`, `_MB`, `_PX`, `_TOPK`）；
- 是否有“为什么是这个值”的注释或文档引用；
- 是否复用已有常量，避免同义重复定义。
