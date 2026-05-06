from abc import ABC, abstractmethod
from typing import List
from schemas.rag import Chunk

class BaseReranker(ABC):
    """
    重排序模型组件的抽象基类 (接口)。
    定义了所有 Reranker 实现类必须遵循的统一规范。
    """

    @abstractmethod
    async def rerank(self, query: str, chunks: List[Chunk]) -> List[Chunk]:
        """
        根据查询对检索到的文本块列表进行重排序。

        Args:
            query (str): 用户的原始查询。
            chunks (List[Chunk]): 经过初步检索召回的文本块列表。

        Returns:
            List[Chunk]: 经过重排序后，相关性更高、顺序更优的文本块列表。
        """
        pass

    def rerank_sync(self, query: str, chunks: List[Chunk]) -> List[Chunk]:
        """
        同步重排序接口（用于非 async 路径）。
        默认实现不提供，请在具体实现类中覆盖。
        """
        raise NotImplementedError("rerank_sync is not implemented")
