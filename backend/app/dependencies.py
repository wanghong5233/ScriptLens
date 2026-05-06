"""
FastAPI 依赖注入模块。

提供各种服务和组件的单例实例，供 API 路由使用。
"""

from service.rag_service import RAGService
from service.core.components_factory import get_embedder, get_reranker, get_llm, get_vector_store

_rag_service_instance = None

def get_rag_service() -> RAGService:
    """
    获取 RAGService 的单例实例。
    
    这个函数会被 FastAPI 的 Depends 机制调用，
    为每个需要 RAGService 的 API 路由自动注入实例。
    
    Returns:
        RAGService: 配置好的 RAG 服务实例
    """
    global _rag_service_instance
    if _rag_service_instance is None:
        # 从组件工厂获取所有必需的组件
        embedder = get_embedder()
        reranker = get_reranker()
        llm = get_llm()
        vector_store = get_vector_store()
        
        # 创建 RAGService 实例
        _rag_service_instance = RAGService(
            embedder=embedder,
            reranker=reranker,
            llm=llm,
            vector_store=vector_store
        )
    
    return _rag_service_instance

