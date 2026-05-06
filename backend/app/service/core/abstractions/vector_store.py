from abc import ABC, abstractmethod
from typing import List, Tuple
from schemas.rag import Chunk

class BaseVectorStore(ABC):
    """
    向量存储组件的抽象基类 (接口)。
    定义了所有向量数据库实现类必须遵循的统一规范。
    """

    @abstractmethod
    async def add_chunks(self, chunks: List[Chunk], index_name: str = None) -> List[str]:
        """
        向向量数据库中添加一批文本块。
        这个方法应该是幂等的，即重复添加相同的 chunk_id 不会产生副作用。

        Args:
            chunks (List[Chunk]): 需要添加的文本块对象列表，每个对象应包含嵌入向量。
            index_name (str, optional): 目标索引名称。如果为 None，使用默认索引。

        Returns:
            List[str]: 成功添加的文本块的 ID 列表。
        """
        pass

    @abstractmethod
    async def search(self, query_embedding: List[float], top_k: int, index_name: str = None) -> List[Tuple[Chunk, float]]:
        """
        根据查询向量，在数据库中进行 Top-K 相似度搜索。

        Args:
            query_embedding (List[float]): 查询的嵌入向量。
            top_k (int): 需要检索的最相似的文本块数量。
            index_name (str, optional): 目标索引名称。如果为 None，使用默认索引。

        Returns:
            List[Tuple[Chunk, float]]: 一个元组列表，每个元组包含一个检索到的文本块
                                      及其与查询的相似度得分。
        """
        pass

    @abstractmethod
    async def delete_by_document_id(self, document_id: str, index_name: str = None) -> None:
        """
        根据文档ID，删除该文档关联的所有文本块。
        这在重新处理或删除知识库文档时非常有用。

        Args:
            document_id (str): 需要删除其所有文本块的文档ID。
            index_name (str, optional): 目标索引名称。如果为 None，使用默认索引。
        """
        pass
