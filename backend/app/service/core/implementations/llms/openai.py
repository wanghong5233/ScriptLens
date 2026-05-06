from typing import List, AsyncGenerator
from openai import AsyncOpenAI
from schemas.rag import Chunk
from service.core.abstractions.llm import BaseLLM
from core.config import settings
from utils.get_logger import log

class OpenAiLlm(BaseLLM):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )
        self.model_name = settings.OPENAI_MODEL_NAME
        log.info(f"OpenAiLlm initialized with model: {self.model_name}")

    def _build_prompt(self, query: str, context: List[Chunk]) -> str:
        # 这个方法将被废弃，逻辑移至 RAGService
        pass

    async def generate(self, query: str, context: List[Chunk]) -> str:
        # 保留此方法以兼容旧代码
        prompt = self._build_prompt_with_citations(query, context)
        return await self.generate_from_prompt(prompt)

    async def stream_generate(self, query: str, context: List[Chunk]) -> AsyncGenerator[str, None]:
        # 保留此方法以兼容旧代码
        prompt = self._build_prompt_with_citations(query, context)
        async for chunk in self.stream_generate_from_prompt(prompt):
            yield chunk

    async def generate_from_prompt(self, prompt: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                stream=False
            )
            return response.choices[0].message.content
        except Exception as e:
            log.error(f"Error generating from OpenAI: {e}", exc_info=True)
            return "对不起，调用模型服务时出错。"

    async def stream_generate_from_prompt(self, prompt: str) -> AsyncGenerator[str, None]:
        try:
            stream = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                stream=True
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            log.error(f"Error streaming from OpenAI: {e}", exc_info=True)
            yield "对不起，调用模型服务时出错。"

    async def generate_with_tools(self, query: str, context: List[Chunk], tools: List) -> dict:
        raise NotImplementedError("Tools support is not implemented for OpenAiLlm yet.")

    def _build_prompt_with_citations(self, query: str, context_chunks: List[Chunk]) -> str:
        # 临时的辅助方法
        formatted_references = [f"[{i+1}] {chunk.content}" for i, chunk in enumerate(context_chunks)]
        references_text = "\n\n".join(formatted_references) if formatted_references else "无参考内容"
        return f"参考内容：\n{references_text}\n\n用户问题：\n{query}\n\n你的回答："
