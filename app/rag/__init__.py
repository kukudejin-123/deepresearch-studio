"""
本地向量知识库（RAG）包

用 FAISS 向量库 + OpenAI 兼容 Embedding 接口替代原来的外部 RAGFlow 服务，
让知识库检索能力完全本地化：文档入库、检索、引用都可以在项目内闭环完成。
"""

from app.rag.embeddings import get_embeddings
from app.rag.vector_store import CollectionInfo, VectorKnowledgeBase

__all__ = [
    "CollectionInfo",
    "VectorKnowledgeBase",
    "get_embeddings",
]
