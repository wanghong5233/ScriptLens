from fastapi import APIRouter, Query
from typing import Any, Dict

from service.core.system.config_service import ConfigService

router = APIRouter()


@router.get("/feature-flags")
def get_feature_flags():
    service = ConfigService()
    return service.get_feature_flags()


@router.get("/parsing-health")
def parsing_health() -> Dict[str, Any]:
    """轻量自检：解析链路关键依赖可用性。
    不读取真实文件，避免重 IO/CPU。
    """
    service = ConfigService()
    return service.parsing_health()


@router.get("/llm-models")
def llm_models(refresh: bool = Query(False, description="是否跳过缓存重新探测 provider 模型目录")) -> Dict[str, Any]:
    """Return LLM model options and availability for frontend selectors."""

    service = ConfigService()
    return service.llm_models(refresh=refresh)
