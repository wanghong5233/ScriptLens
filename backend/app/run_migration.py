#!/usr/bin/env python3
"""
Alembic迁移运行脚本
用于在Docker环境中运行数据库迁移
"""

import sys
from alembic.config import Config
from alembic import command
from core.config import settings


def run_migration():
    """运行数据库迁移"""

    database_url = settings.DATABASE_URL
    if not database_url:
        print("错误: 未找到DATABASE_URL环境变量")
        sys.exit(1)

    print(f"数据库连接: {database_url}")

    alembic_cfg = Config("alembic.ini")

    try:
        print("检查当前数据库版本...")
        command.current(alembic_cfg, verbose=True)

        print("开始运行数据库迁移...")
        command.upgrade(alembic_cfg, "head")

        print("数据库迁移完成!")

    except Exception as e:
        print(f"迁移失败: {str(e)}")
        sys.exit(1)


def stamp_baseline():
    """标记baseline版本（首次运行时使用）"""

    alembic_cfg = Config("alembic.ini")

    try:
        print("标记baseline版本...")
        command.stamp(alembic_cfg, "980b32f130df")
        print("Baseline标记完成!")

    except Exception as e:
        print(f"标记baseline失败: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stamp":
        stamp_baseline()
    else:
        run_migration() 