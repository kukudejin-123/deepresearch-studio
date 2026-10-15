"""
Embedding 模型初始化模块

统一提供文本向量化能力，默认复用 .env 中的 OpenAI 兼容服务
（OPENAI_BASE_URL / OPENAI_API_KEY），模型名由 EMBEDDING_MODEL 控制，
例如阿里云百炼的 text-embedding-v3。

第三期性能优化：带 LRU 缓存的 Embedding 包装。
向量化是纯函数（同一文本永远得到同一向量），而知识库检索场景中
相同问题/相同文档分片会被反复向量化，缓存可直接省掉这部分 API
调用与延迟。缓存为进程内实现，多 worker 部署时各进程独立。
"""

import os
import threading
from collections import OrderedDict
from typing import Any, Optional

from dotenv import find_dotenv, load_dotenv
from langchain_openai import OpenAIEmbeddings

from app.utils.logging_config import get_logger

load_dotenv(find_dotenv())

logger = get_logger(__name__)

# 默认嵌入模型：百炼 text-embedding-v3，中文场景效果与成本都比较均衡
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"

# 模块级复用同一个 embeddings 实例，避免每次检索都重建 HTTP 客户端
_embeddings: Optional["CachedEmbeddings"] = None


class CachedEmbeddings:
    """
    带 LRU 缓存的 Embedding 包装

    实现 langchain 的 Embeddings 协议（同步 + 异步），对上层透明；
    缓存命中率可通过日志观察（DEBUG 级别）。
    """

    def __init__(self, base: OpenAIEmbeddings, max_size: int = 1024):
        """
        :param base: 底层 Embedding 客户端
        :param max_size: 缓存条目上限（LRU 淘汰）
        """
        self._base = base
        self._max_size = max_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    # langchain 的 FAISS 等组件会读取 model 属性
    @property
    def model(self) -> str:
        return getattr(self._base, "model", "")

    def _get(self, text: str) -> Optional[list[float]]:
        with self._lock:
            vector = self._cache.get(text)
            if vector is not None:
                self._cache.move_to_end(text)
                self.hits += 1
            return vector

    def _put(self, text: str, vector: list[float]) -> None:
        with self._lock:
            self._cache[text] = vector
            self._cache.move_to_end(text)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            self.misses += 1

    @staticmethod
    def _finalize(results: list) -> list:
        """
        校验并输出结果

        任何位置缺失向量都直接抛错：静默过滤会让返回向量与输入文本
        错位，FAISS 会拿到错误的分片向量且极难排查。
        """
        vectors = []
        for vector in results:
            if vector is None:
                raise RuntimeError("Embedding 结果与输入文本数量不一致")
            vectors.append(vector)
        return vectors

    # ---- 同步接口 ----

    def embed_query(self, text: str) -> list[float]:
        cached = self._get(text)
        if cached is not None:
            return cached
        vector = self._base.embed_query(text)
        self._put(text, vector)
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        results: list[Optional[list[float]]] = [None] * len(texts)
        missing: list[int] = []

        for index, text in enumerate(texts):
            cached = self._get(text)
            if cached is not None:
                results[index] = cached
            else:
                missing.append(index)

        if missing:
            fresh = self._base.embed_documents([texts[i] for i in missing])
            for position, index in enumerate(missing):
                vector = fresh[position]
                self._put(texts[index], vector)
                results[index] = vector

        return self._finalize(results)

    # ---- 异步接口（LangGraph/FAISS 检索路径实际调用） ----

    async def aembed_query(self, text: str) -> list[float]:
        cached = self._get(text)
        if cached is not None:
            return cached
        vector = await self._base.aembed_query(text)
        self._put(text, vector)
        return vector

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        results: list[Optional[list[float]]] = [None] * len(texts)
        missing: list[int] = []

        for index, text in enumerate(texts):
            cached = self._get(text)
            if cached is not None:
                results[index] = cached
            else:
                missing.append(index)

        if missing:
            fresh = await self._base.aembed_documents([texts[i] for i in missing])
            for position, index in enumerate(missing):
                vector = fresh[position]
                self._put(texts[index], vector)
                results[index] = vector

        return self._finalize(results)

    def stats(self) -> dict[str, int]:
        """缓存统计（命中/未命中/当前大小）"""
        return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}


def get_embeddings() -> CachedEmbeddings:
    """
    获取全局复用的 Embedding 客户端（惰性初始化，带缓存）

    采用惰性初始化：导入阶段不创建客户端，未配置 API Key 时只在真正
    调用向量化时才报错，保证不依赖知识库的链路（如网络搜索）不受影响。

    :return: 带 LRU 缓存的 Embedding 客户端
    """
    global _embeddings
    if _embeddings is None:
        kwargs: dict[str, Any] = {
            "model": os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key

        try:
            cache_size = int(os.getenv("EMBEDDING_CACHE_SIZE", "1024"))
        except ValueError:
            cache_size = 1024

        _embeddings = CachedEmbeddings(OpenAIEmbeddings(**kwargs), max_size=cache_size)
        logger.info(
            "Embedding 客户端已初始化: model=%s, base_url=%s, 缓存容量=%d",
            kwargs["model"],
            base_url or "默认",
            cache_size,
        )
    return _embeddings
