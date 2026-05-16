# Skill Examples · 反例 → 正例

## Description 字段

### 反例 1：太短 + 无 when + 无关键词

```yaml
---
name: pdf-tools
description: Helps with PDFs.
---
```

问题：模型完全不知道何时触发；触发率几乎为 0。

### 反例 2：太长 + 关键词淹没

```yaml
---
name: pdf-tools
description: >-
  A comprehensive utility for working with PDF documents that supports
  extraction, manipulation, form filling, merging, splitting, OCR, signature
  validation, and a variety of other tasks that the user may need when
  dealing with PDF documents in their daily workflows or automation pipelines.
---
```

问题：(a) 多行 YAML 在 Claude Code 中无法识别；(b) >400 字符稀释关键词权重；(c) 无 when，无 negative trigger。

### 正例

```yaml
---
name: pdf-tools
description: Extract text and tables from PDF files, fill PDF forms, merge multiple PDFs. Use when the user mentions PDF, .pdf, document extraction, form filling, 表单, OCR 扫描件. Do NOT use for Word / Excel / Markdown or non-PDF document formats.
---
```

要点：单行 / what 在前 / when 在中（含中英文同义词）/ negative trigger 在末 / ~250 字符。

## Body 结构

### 反例：堆 MUST、无 why、无例子

```markdown
# PDF Tools

## Rules

- ALWAYS check file extension first
- NEVER process files larger than 100MB
- MUST validate PDF signature
- ALWAYS use streaming for large files
- NEVER load entire PDF into memory
- MUST handle encrypted PDFs by asking user for password
- ALWAYS log file path
```

问题：纯规则、无原因、模型遇到未列场景就僵化；7 个 MUST/NEVER/ALWAYS 是 yellow flag。

### 正例：流程 + reasoning + 具体例子

```markdown
# PDF Tools

## 处理流程

1. **读前验证**：检查扩展名 + PDF 签名（`%PDF-` 开头）
   - why：错误扩展名会让后续库抛晦涩异常，先校验更易定位
2. **选择读取模式**：
   - <50 MB：直接加载
   - ≥50 MB：流式读取
   - why：内存型解析对大文件会 OOM；流式略慢但稳定
3. **处理加密**：检测到加密标志位时主动询问密码，不静默失败

## 例子

输入：`扫描件 contract.pdf, 32 MB, 加密`
动作：直接加载 → 检测加密 → 请求密码 → OCR
```

要点：流程化、附 why、给具体输入输出。

## Progressive Disclosure 拆分

### 反例：所有变体堆在 SKILL.md（200+ 行）

```markdown
# Cloud Deploy

## AWS Deployment
（80 行 AWS 细节）

## GCP Deployment
（80 行 GCP 细节）

## Azure Deployment
（80 行 Azure 细节）
```

问题：用户只用 AWS 时，GCP+Azure 的 160 行也每次入 context。

### 正例：SKILL.md 写选择逻辑，细节进 references/

```markdown
# Cloud Deploy

## Deployment Workflow

1. Identify cloud provider from user requirements
2. Load the matching reference:
   - AWS → `references/aws.md`
   - GCP → `references/gcp.md`
   - Azure → `references/azure.md`
3. Follow provider-specific steps from the loaded reference
```

```text
cloud-deploy/
├── SKILL.md              (~40 行)
├── references/
│   ├── aws.md            (按需读)
│   ├── gcp.md            (按需读)
│   └── azure.md          (按需读)
```

效果：context 节省 ~60%；每次只加载相关变体。

## Frontmatter Name 字段

| 反例 | 原因 |
|---|---|
| `My-Skill` | 含大写 |
| `my_skill` | 含下划线 |
| `-pdf` | 首字符为 `-` |
| `pdf--tools` | 连续 `--` |
| 目录名为 `pdf-tools`，但 `name: tools` | 与目录名不匹配 |

正例：`pdf-tools` / `cloud-deploy` / `writing-skill`
