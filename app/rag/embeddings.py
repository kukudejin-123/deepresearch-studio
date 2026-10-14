"""
Embedding 模型初始化模块

统一提供文本向量化能力，默认复用 .env 中的 OpenAI 兼容服务
（OPENAI_BASE_URL / OPENAI_API_KEY），模型名由 EMBEDDING_MODEL 控制，
例如阿里云百炼的 text-embedding-v3。
"""

import os
from typing import Any, Optional

from dotenv import find_dotenv, load_dotenv
from langchain_openai import OpenAIEmbeddings

from app.utils.logging_config import get_logger

load_dotenv(find_dotenv())

logger = get_logger(__name__)

# 默认嵌入模型：百炼 text-embedding-v3，中文场景效果与成本都比较均衡
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"

# 模块级复用同一个 embeddings 实例，避免每次检索都重建 HTTP 客户端
_embeddings: Optional[OpenAIEmbeddings] = None


def get_embeddings() -> OpenAIEmbeddings:
    """
    获取全局复用的 Embedding 客户端

    采用惰性初始化：导入阶段不创建客户端，未配置 API Key 时只在真正
    调用向量化时才报错，保证不依赖知识库的链路（如网络搜索）不受影响。

    :return: OpenAI 兼容接口的 Embedding 客户端
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
        _embeddings = OpenAIEmbeddings(**kwargs)
        logger.info(
            "Embedding 客户端已初始化: model=%s, base_url=%s",
            kwargs["model"],
            base_url or "默认",
        )
    return _embeddings
