from abc import ABC, abstractmethod
from typing import List
from schemas.rag import Document, Chunk

class BaseEmbedder(ABC):
    """
    嵌入模型组件的抽象基类 (接口)。
    定义了所有嵌入模型实现类必须遵循的统一规范。
    """

    @abstractmethod
    async def embed_documents(self, documents: List[Document]) -> List[Chunk]:
        """
        对一批文档进行分块和嵌入。

        Args:
            documents (List[Document]): 需要处理的文档对象列表。

        Returns:
            List[Chunk]: 处理后生成的、包含嵌入向量的文本块列表。
        """
        pass

    @abstractmethod
    async def embed_chunks(self, chunks: List[Chunk]) -> List[Chunk]:
        """
        对一批已经分好的文本块进行嵌入。

        Args:
            chunks (List[Chunk]): 需要进行嵌入的文本块对象列表。

        Returns:
            List[Chunk]: 更新了嵌入向量的文本块列表。
        """
        pass

    @abstractmethod
    async def embed_query(self, query: str) -> List[float]:
        """
        对单个查询字符串进行嵌入。

        Args:
            query (str): 用户的查询问题。

        Returns:
            List[float]: 查询问题的嵌入向量。
        """
        pass
