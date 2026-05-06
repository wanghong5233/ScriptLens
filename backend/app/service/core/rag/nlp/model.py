from openai import OpenAI
from llama_index.core.data_structs import Node
from llama_index.core.schema import NodeWithScore
from llama_index.postprocessor.dashscope_rerank import DashScopeRerank
import numpy as np
import logging
import threading
import time
from typing import List, Optional

from core.config import settings

logger = logging.getLogger("rag.embedding")
_LOCAL_EMBEDDER_MODEL = None
_LOCAL_EMBEDDER_LOCK = threading.Lock()
_LOCAL_EMBEDDER_DISABLED_REASON: Optional[str] = None
_LOCAL_EMBEDDER_FALLBACK_WARNED = False


def get_chat_completion_block(session_id, question, references):
    try:
        client = OpenAI(
            api_key=settings.DASHSCOPE_API_KEY,
            base_url=settings.DASHSCOPE_BASE_URL,
        )
        formatted_references = "\n".join([f"[{ref['id']}] {ref['content']}" for ref in references])
        prompt = f"Question: {question}\nReferences:\n{formatted_references}\nAnswer concisely with citations."
        completion = client.chat.completions.create(
            model="deepseek-r1",
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"


def rerank_similarity(query, texts):
    api_key = settings.DASHSCOPE_API_KEY
    nodes = [NodeWithScore(node=Node(text=text), score=1.0) for text in texts]
    dashscope_rerank = DashScopeRerank(top_n=len(texts), api_key=api_key)
    results = dashscope_rerank.postprocess_nodes(nodes, query_str=query)
    scores = [res.score for res in results]
    return np.array(scores), None


def _get_local_embedder():
    """本地 embedder 路径，与 LocalBgeEmbedder 同属"暂未启用、预留扩展"分支。

    当前镜像不安装 sentence-transformers / torch，SM_EMBEDDER_TYPE=local 不会
    被 components_factory 选中。本函数保留是因为 `generate_embedding()` 内仍有
    `if SM_EMBEDDER_TYPE=='local'` 兜底逻辑：万一配置漂移到 local，这里会因找
    不到 sentence_transformers 抛 ImportError，被上层 try/except 捕获并自动
    降级到远程 DashScope，不会让请求 500。
    """
    global _LOCAL_EMBEDDER_MODEL, _LOCAL_EMBEDDER_DISABLED_REASON
    if _LOCAL_EMBEDDER_DISABLED_REASON:
        raise RuntimeError(_LOCAL_EMBEDDER_DISABLED_REASON)
    if _LOCAL_EMBEDDER_MODEL is None:
        with _LOCAL_EMBEDDER_LOCK:
            if _LOCAL_EMBEDDER_MODEL is None:
                from sentence_transformers import SentenceTransformer  # noqa: F401  (inactive path; see docstring)
                try:
                    _LOCAL_EMBEDDER_MODEL = SentenceTransformer(
                        settings.LOCAL_EMBEDDER_PATH,
                        trust_remote_code=True,
                        device=settings.SM_LOCAL_EMBEDDER_DEVICE,
                    )
                    try:
                        logger.info(
                            "Local embedder loaded: %s (%s)",
                            settings.LOCAL_EMBEDDER_PATH,
                            settings.SM_LOCAL_EMBEDDER_DEVICE,
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    _LOCAL_EMBEDDER_DISABLED_REASON = f"Local embedder unavailable: {exc}"
                    raise
    return _LOCAL_EMBEDDER_MODEL


def _generate_local_embedding(text: str | List[str], max_batch_size: int) -> Optional[List[float] | List[List[float]]]:
    model = _get_local_embedder()
    if isinstance(text, str):
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    if isinstance(text, list):
        vecs = model.encode(text, normalize_embeddings=True, batch_size=max_batch_size)
        return [vec.tolist() for vec in vecs]
    return None


def _generate_remote_embedding(
    text: str | List[str],
    *,
    api_key: Optional[str],
    base_url: Optional[str],
    model_name: str,
    dimensions: int,
    encoding_format: str,
    max_batch_size: int,
) -> Optional[List[float] | List[List[float]]]:
    client = OpenAI(api_key=api_key, base_url=base_url)

    def _request_one(input_data):
        try:
            completion = client.embeddings.create(
                model=model_name,
                input=input_data,
                dimensions=dimensions,
                encoding_format=encoding_format,
            )
            return completion
        except Exception:
            # 某些模型/网关不支持 dimensions 或 encoding_format，降级重试一次。
            completion = client.embeddings.create(
                model=model_name,
                input=input_data,
            )
            return completion

    if isinstance(text, str):
        try:
            completion = _request_one(text)
            return completion.data[0].embedding
        except Exception as e:
            try:
                logger.warning("Remote embedding request failed: %s", e)
            except Exception:
                pass
            return None

    if isinstance(text, list):
        all_embeddings: List[List[float] | None] = []
        total = len(text)
        batches = (total + max_batch_size - 1) // max_batch_size if max_batch_size > 0 else 0
        started_at = time.perf_counter()
        logger.info(
            "Remote embedding batches start total=%s batches=%s batch_size=%s model=%s",
            total,
            batches,
            max_batch_size,
            model_name,
        )
        for i in range(0, len(text), max_batch_size):
            batch = text[i : i + max_batch_size]
            batch_started_at = time.perf_counter()
            batch_no = i // max_batch_size + 1
            try:
                completion = _request_one(batch)
                batch_embeddings = [item.embedding for item in completion.data]
                all_embeddings.extend(batch_embeddings)
                logger.info(
                    "Remote embedding batch ok batch=%s/%s size=%s elapsed_ms=%s",
                    batch_no,
                    batches,
                    len(batch),
                    int((time.perf_counter() - batch_started_at) * 1000),
                )
            except Exception as e:
                try:
                    logger.warning("Remote embedding batch failed (batch %s): %s", batch_no, e)
                except Exception:
                    pass
                all_embeddings.extend([None] * len(batch))
        logger.info(
            "Remote embedding batches finish total=%s returned=%s elapsed_ms=%s",
            total,
            len(all_embeddings),
            int((time.perf_counter() - started_at) * 1000),
        )
        return all_embeddings
    return None


def generate_embedding(
    text: str | List[str],
    api_key: str | None = None,
    base_url: str | None = None,
    model_name: str | None = None,
    dimensions: int | None = None,
    encoding_format: str | None = None,
    max_batch_size: int | None = None,
):
    global _LOCAL_EMBEDDER_FALLBACK_WARNED
    model_name = model_name or str(getattr(settings, "SM_EMBEDDING_MODEL", "text-embedding-v3") or "text-embedding-v3")
    dimensions = int(dimensions if dimensions is not None else int(getattr(settings, "SM_EMBEDDING_DIMENSIONS", 1024) or 1024))
    encoding_format = encoding_format or str(getattr(settings, "SM_EMBEDDING_ENCODING_FORMAT", "float") or "float")
    max_batch_size = int(max_batch_size if max_batch_size is not None else int(getattr(settings, "SM_EMBEDDING_MAX_BATCH_SIZE", 10) or 10))
    if getattr(settings, "SM_EMBEDDER_TYPE", "dashscope") == "local":
        try:
            return _generate_local_embedding(text, max_batch_size)
        except Exception as e:
            try:
                if not _LOCAL_EMBEDDER_FALLBACK_WARNED:
                    logger.warning("Local embedder failed, fallback to remote: %s", e)
                    _LOCAL_EMBEDDER_FALLBACK_WARNED = True
                else:
                    logger.debug("Local embedder unavailable, fallback to remote: %s", e)
            except Exception:
                pass

    api_key = api_key or settings.DASHSCOPE_API_KEY
    base_url = base_url or settings.DASHSCOPE_BASE_URL
    return _generate_remote_embedding(
        text,
        api_key=api_key,
        base_url=base_url,
        model_name=model_name,
        dimensions=dimensions,
        encoding_format=encoding_format,
        max_batch_size=max_batch_size,
    )


if __name__ == "__main__":
    question = "法国的首都是哪里？"
    references = [
        {"id": 1, "content": "法国的首都是巴黎。"},
        {"id": 2, "content": "巴黎是欧洲的文化中心之一。"},
    ]
    session_id = "sd"
    response = get_chat_completion_block(session_id, question, references)
    print(response)