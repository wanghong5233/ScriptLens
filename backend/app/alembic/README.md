# Alembic 数据库迁移指南

本文档为 ScholarMind 项目提供了使用 Alembic 管理数据库模式迁移的操作说明。

## 简介

Alembic 是一个专为 SQLAlchemy 设计的数据库迁移工具。它使我们能够以一种结构化、版本化的方式，来管理数据库模式（schema）随时间发生的变化。每当我们修改 SQLAlchemy 模型（例如，添加新表、新列）时，都需要创建一个迁移脚本来将这些变更应用到数据库中。

## 执行环境

所有 Alembic 命令都**必须**在 `scholarmind_api` Docker 容器内部执行，以确保命令在正确的数据库连接和应用上下文中运行。

所有命令的标准前缀是：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic <command>"
```

## 常用命令

以下是最高频使用的 Alembic 命令。

### 检查当前状态

查看数据库当前的迁移版本：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic current"
```

### 查看迁移历史

查看所有的迁移历史记录：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic history --verbose"
```

### 应用迁移 (升级)

将数据库升级到最新的版本：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic upgrade head"
```

升级到指定的版本：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic upgrade <revision_id>"
```

### 回滚迁移 (降级)

将数据库降级一个版本（请谨慎使用）：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic downgrade -1"
```

降级到指定的版本：
```bash
docker compose exec scholarmind_api bash -lc "cd /app && alembic downgrade <revision_id>"
```

### 生成新的迁移脚本

当您修改了 `backend/app/models/` 目录下的 SQLAlchemy 模型后，可以请求 Alembic 自动生成迁移脚本。

1.  **生成脚本**:
    ```bash
    docker compose exec scholarmind_api bash -lc "cd /app && alembic revision --autogenerate -m '添加关于变更的描述性信息'"
    ```
    请将 `'添加关于变更的描述性信息'` 替换为对本次模式修改的简短、有意义的描述（例如：`'为 knowledgebases 表添加 is_ephemeral 列'`）。

2.  **审查脚本**:
    一个新的迁移文件将在 `backend/app/alembic/versions/` 目录下被创建。**您必须手动审查这个生成的文件**，以确保它准确地反映了您预期的变更。自动生成功能很强大，但并非完美无缺。

3.  **应用迁移**:
    当您对脚本感到满意后，使用 `upgrade` 命令来应用它。