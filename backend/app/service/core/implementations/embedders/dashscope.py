from typing import List
from openai import AsyncOpenAI
from schemas.rag import Document, Chunk
from service.core.abstractions.embedder import BaseEmbedder
from core.config import settings
from utils.get_logger import log
from exceptions.base import APIException

class DashScopeEmbedder(BaseEmbedder):
    """
    使用阿里云通义千问 (DashScope) API 来生成嵌入向量的实现类。
    它通过 OpenAI 的 SDK 以兼容模式进行调用。
    """
    def __init__(self):
        if not settings.DASHSCOPE_API_KEY:
            raise ValueError("DASHSCOPE_API_KEY is not set in the environment.")
        
        self.client = AsyncOpenAI(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL
        )
        self.model_name = str(getattr(settings, "SM_EMBEDDING_MODEL", "text-embedding-v3") or "text-embedding-v3")
        log.info(f"DashScopeEmbedder initialized model={self.model_name}.")

    async def embed_documents(self, documents: List[Document]) -> List[Chunk]:
        log.warning("embed_documents is not fully implemented in DashScopeEmbedder. It returns an empty list.")
        return []

    async def _embed_with_retry(self, texts: List[str]) -> List[List[float]]:
        try:
            response = await self.client.embeddings.create(
                model=self.model_name,
                input=texts
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            log.error(f"DashScope API request failed: {e}", exc_info=True)
            raise APIException(status_code=500, message="Failed to get embeddings from DashScope.")

    async def embed_chunks(self, chunks: List[Chunk]) -> List[Chunk]:
        contents = [chunk.content for chunk in chunks]
        log.info(f"Embedding {len(contents)} chunks using DashScope API.")
        
        embeddings = await self._embed_with_retry(contents)
        
        for chunk, embedding in zip(chunks, embeddings):
            chunk.embedding = embedding
            
        return chunks

    async def embed_query(self, query: str) -> List[float]:
        log.info(f"Embedding query using DashScope API: '{query[:50]}...'")
        embeddings = await self._embed_with_retry([query])
        return embeddings[0]
