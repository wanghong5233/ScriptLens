# Error Codes（Script API）

## 目标

统一 Script API 错误响应协议，避免前后端依赖错误文案字符串匹配。

- 响应结构：`detail = {"code": string, "message": string}`
- 适用范围：`backend/app/router/script_rt.py`
- 前端消费入口：`frontend/src/pages/doc-studio/index.tsx` 的 `getErrorCode()` / `getOperationErrorMessage()`

## 响应契约

- HTTP 状态码表达传输层语义（404/403/409/400/413）。
- `detail.code` 表达稳定业务语义（供前端分流）。
- `detail.message` 面向用户展示，可本地化、可调整。

示例：

```json
{
  "detail": {
    "code": "SCRIPT_NOT_FOUND",
    "message": "剧本不存在或无权限访问"
  }
}
```

## 错误码清单

| code | HTTP | 触发场景（后端） | 前端建议提示 |
|---|---:|---|---|
| `SCRIPT_NOT_FOUND` | 404 | script 不存在或不属于当前用户 | 当前剧本不存在或你没有访问权限 |
| `SCRIPT_FORBIDDEN` | 403 | 有资源但当前用户无访问/导出权限 | 你没有权限访问该剧本 |
| `SCRIPT_NOT_READY` | 409 | 剧本状态不是 `ready`（chat/rewrite/reanalyze/view） | 剧本尚未就绪，请稍后重试 |
| `REPORT_NOT_READY` | 409 | `view` 请求时报告尚未生成 | 评分报告正在生成，请稍后刷新 |
| `SCENE_NOT_FOUND` | 404 | scene 不存在或不属于该 script | 目标场景不存在或你没有访问权限 |
| `INVALID_SCENE_CONTENT` | 400 | scene 写回内容非法（空内容/参数不合法） | 场景内容不合法，请检查后重试 |
| `UNSUPPORTED_SCRIPT_FORMAT` | 400 | 上传文件格式不支持 | 不支持该文件格式，请转为 docx/pdf/txt/md |
| `UPLOAD_TOO_LARGE` | 413 | 上传文件超出大小限制 | 文件过大，请压缩或拆分后重试 |
| `EMPTY_UPLOAD` | 400 | 上传文件为空 | 上传文件为空，请重新选择文件 |
| `INVALID_EXPORT_REQUEST` | 400 | 导出参数非法（format 等） | 导出参数不合法，请检查后重试 |
| `REWRITE_FAILED` | 400 | 单场改写工具执行失败 | 改写失败，请调整指令后重试 |
| `INVALID_FEEDBACK_REQUEST` | 400 | feedback 参数不合法 | 反馈参数不合法，请检查后重试 |
| `OPERATION_NOT_FOUND` | 404 | operation 引用不存在 | 该操作记录不存在，可能已过期 |
| `OPERATION_FORBIDDEN` | 403 | operation 不属于当前用户 | 你没有权限访问该操作记录 |
| `INVALID_OPERATION_REQUEST` | 400 | operation 参数非法（如 `operation_id` 协议错误） | 操作请求格式非法，请刷新后重试 |

## 约束

- 新增 API 错误码时，必须同时更新：
  - 本文档（错误码表）
  - 前端错误映射（`getOperationErrorMessage()`）
  - 对应路由单元/集成测试（若有）
- 禁止回退到裸字符串 `detail="..."` 形式。

## 关联文档

- `docs/11-operation-id-protocol.md`
