from core.config import settings
from service.core.abstractions.embedder import BaseEmbedder
from service.core.abstractions.reranker import BaseReranker
from service.core.abstractions.llm import BaseLLM
from service.core.abstractions.vector_store import BaseVectorStore
from exceptions.base import ModelNotFoundError

# ScriptLens MVP：仅启用基于 API 的实现（DashScope / OpenAI），不打包本地 embedder / reranker / LLM。
from service.core.implementations.embedders.dashscope import DashScopeEmbedder
from service.core.implementations.llms.dashscope import DashScopeLlm
from service.core.implementations.llms.openai import OpenAiLlm

# 这是一个简单的“注册表”模式，用于缓存已创建的组件实例（单例）
_embedder_instance = None
_reranker_instance = None
_llm_instance = None
_vector_store_instance = None

def get_embedder() -> BaseEmbedder:
    """
    组件工厂函数：根据配置返回一个 BaseEmbedder 的单例。
    """
    global _embedder_instance
    if _embedder_instance is None:
        if settings.SM_EMBEDDER_TYPE == "dashscope":
            _embedder_instance = DashScopeEmbedder()
        # SM_EMBEDDER_TYPE=local 暂未启用（预留扩展点，详见模块顶部说明）
        # elif settings.SM_EMBEDDER_TYPE == "local":
        #     _embedder_instance = LocalBgeEmbedder()
        else:
            raise ModelNotFoundError(
                model_name=settings.SM_EMBEDDER_TYPE,
                message=(
                    f"Embedder type '{settings.SM_EMBEDDER_TYPE}' is not enabled in this image. "
                    "Currently only 'dashscope' is built in. "
                    "To enable 'local', see the activation steps in components_factory.py header."
                ),
            )
    return _embedder_instance

def get_reranker() -> BaseReranker:
    """ScriptLens MVP 不使用 reranker（六层 RAG 简化为 embedding+BM25 → RRF）。"""
    raise ModelNotFoundError(
        model_name="reranker",
        message="ScriptLens MVP disables reranker; use embedding+BM25+RRF only.",
    )

def get_llm() -> BaseLLM:
    """组件工厂函数：根据配置返回一个 BaseLLM 的单例。"""
    global _llm_instance
    if _llm_instance is None:
        if settings.SM_LLM_TYPE == "dashscope":
            _llm_instance = DashScopeLlm()
        elif settings.SM_LLM_TYPE == "openai":
            _llm_instance = OpenAiLlm()
        else:
            raise ModelNotFoundError(model_name=settings.SM_LLM_TYPE, message="Unknown LLM type configured.")
    return _llm_instance

def get_vector_store() -> BaseVectorStore:
    """组件工厂函数：MVP 仅 pgvector。"""
    global _vector_store_instance
    if _vector_store_instance is None:
        vector_store = str(getattr(settings, "SM_VECTOR_STORE", "pgvector") or "pgvector").strip().lower()
        if vector_store == "pgvector":
            from service.core.implementations.vector_stores.pgvector import PgVectorVectorStore

            _vector_store_instance = PgVectorVectorStore()
        else:
            raise ModelNotFoundError(model_name=vector_store, message="Only pgvector is supported in ScriptLens MVP.")
    return _vector_store_instance
