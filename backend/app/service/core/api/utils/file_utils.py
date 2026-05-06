import os
from typing import Optional
from core.config import settings

# 全局变量，用于缓存项目核心服务(core)的根目录路径。
# 优先从环境变量 RAG_PROJECT_BASE 或 RAG_DEPLOY_BASE 读取，
# 这对于在不同部署环境中配置项目路径非常有用。
PROJECT_BASE: Optional[str] = settings.RAG_PROJECT_BASE or settings.RAG_DEPLOY_BASE


def get_project_base_directory(*args: str) -> str:
    """
    获取并返回项目核心服务(core)的根目录路径，支持拼接子路径。

    该函数旨在提供一个稳定的方式来定位到 `backend/app/service/core` 目录，
    以便在不同模块中安全地引用其下的资源（如 `storage` 目录）。

    策略如下:
    1.  优先使用通过环境变量 `RAG_PROJECT_BASE` 或 `RAG_DEPLOY_BASE` 设定的路径。
    2.  如果环境变量未设置，则根据此文件(__file__)的当前位置推断出
        `backend/app/service/core` 目录的绝对路径。
    3.  计算结果会被缓存到全局变量 `PROJECT_BASE` 中，避免重复计算。

    Args:
        *args (str): 零个或多个路径字符串，会被依次拼接到 `core` 目录后面。

    Returns:
        str: 指向 `backend/app/service/core` 目录的绝对路径。如果提供了`args`，
             则返回拼接后的完整路径。
    """
    global PROJECT_BASE
    # 检查全局变量 PROJECT_BASE 是否已经有值 (通过环境变量或上次计算)
    if PROJECT_BASE is None:
        # 如果没有值，则根据当前文件位置动态计算。
        # 从当前文件 (.../utils) 向上追溯两级来到达 core/ 目录
        # 1. .../utils -> .../api
        # 2. .../api -> .../core
        PROJECT_BASE = os.path.abspath(
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)),
                os.pardir,
                os.pardir
            )
        )

    if args:
        # 如果提供了子路径，则将它们与 core 根目录拼接
        return os.path.join(PROJECT_BASE, *args)
    
    # 如果没有提供子路径，直接返回 core 根目录
    return PROJECT_BASE
