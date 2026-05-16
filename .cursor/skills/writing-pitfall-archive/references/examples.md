# Pitfall Archive Examples · 反例 → 正例

## 反例：教程口吻 + 命令序列 + 决策辩证

```markdown
### 10.10 doc_studio 部署优化

之前我们一直在 ECS 上完整 build doc_studio 镜像，但发现这样会导致 CPU
飙到 95%，SSH 都断了。我们考虑了几个方案：A 升配置（贵），B 调度时间
（不稳定），C 改 Dockerfile（治标）。最后选择了 D：增量构建。

操作步骤如下：
1. 首先确认本地有 base 镜像：
   ```bash
   docker image inspect backend-doc_studio:base
   ```
2. 接下来创建 Dockerfile.fast：
   ```dockerfile
   FROM backend-doc_studio:base
   COPY services/doc_studio/ /app/
   ```
3. 然后 build：
   ```bash
   docker build -f Dockerfile.fast -t backend-doc_studio:latest .
   ```
4. 最后 up：
   ```bash
   docker compose up -d --no-build --no-deps doc_studio
   ```

不用担心 base 镜像，后续会自动维护。
```

问题：教程口吻、5+ 行命令序列、决策辩证、安抚语言全员到齐。读者下次又遇到同类坑无法快速定位根因。

## 正例：硬约束 + 五段式 + 脚本引用

```markdown
### §1.3 BuildKit 缓存 ≠ Docker 镜像层

| 编号 | 约束 | 违反后果 |
|---|---|---|
| 1.3 | BuildKit cache 不是 image layer，`docker buildx prune` 后会丢；ECS 上重新 build texlive 必然 thrash | doc_studio 永远不能在 ECS 上完整 build |

### §4.1 [2026-05-08] doc_studio 在 ECS 完整 build 导致 SSH 断连

- **Symptom**：`docker compose up -d --build doc_studio` 后 CPU 95%+，SSH banner exchange timeout，云助手 agent "未运行"。
- **Evidence**：
  - `docker buildx du` 显示 cache 已被 prune
  - `apt-get install texlive-*` 阶段卡 30 分钟以上
  - 监控：磁盘 IO 110MB/s、1500 IOPS 持续
- **Root Cause**：违反 §1.3。BuildKit cache 丢失 → 重新下载并解包 texlive（多 GB）→ 2C2G 内存不足 → swap thrash。
- **Solution**：`Dockerfile.fast` 复用 `backend-doc_studio:base`，只 COPY 代码；本地 build 完整镜像后 `docker save | scp` 到 ECS。
- **Invariant**：升级 §1.3 — **doc_studio 永远不能在 ECS 上完整 build**。

完整流程见 `backend/scripts/deploy_doc_studio_fast.sh`。
```

## 五段式字段拆解

| 段 | 内容 | 长度 |
|---|---|---|
| **Symptom** | 用户/工程师看到的现象 | 1-2 句 |
| **Evidence** | 可复现的证据：日志关键词、命令输出片段、监控数值 | 2-4 行 |
| **Root Cause** | 为什么会出现，引用 §1 的硬约束编号 | 1-3 句 |
| **Solution** | 改 env / 代码 / 流程 / 架构 | 1-3 句 |
| **Invariant** | 沉淀到 §1 的哪条；新增不变量则在此声明；同类问题在其他项目也见过 → 上抽象到 playbook 并注明 | 1-2 句 |

条目命名：`§4.N [日期] 坑点一句话总结`，按时间倒序排列。
