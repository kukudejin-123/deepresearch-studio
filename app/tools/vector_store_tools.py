"""
本地向量知识库工具模块

封装两个给知识库子智能体使用的 LangChain 工具：
list_knowledge_collections 用于发现本地 FAISS 向量库中有哪些集合，
search_knowledge_base 用于在指定集合中检索与问题相关的文档分片。

替代原 RAGFlow 外部服务后，私有知识库问答完全在项目内闭环。

资源隔离策略（第一期安全改造）：
- 每个用户有独立的向量库目录（VECTOR_STORE_DIR/user_{user_id}），
  自己的文档不会暴露给其他用户；
- 共享目录（VECTOR_STORE_DIR 根下）作为公共知识库对所有用户可见，
  用于开箱即用的演示数据（电商行业、金融行业等）；
- 集合发现时用户私有库优先，重名时以私有库为准。
"""

import os
from typing import Optional

from langchain_core.tools import tool

from app.api.context import get_user_context
from app.api.monitor import monitor
from app.rag.vector_store import CollectionInfo, VectorKnowledgeBase
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 当前文件位于 app/tools/vector_store_tools.py，parents[1] 即 app 目录
_DEFAULT_STORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "vector_store",
)

# 共享知识库根目录：按根路径惰性创建并缓存，集合发现不需要 Embedding 客户端
_shared_knowledge_bases: dict[str, VectorKnowledgeBase] = {}

# 用户私有知识库缓存：user_id -> VectorKnowledgeBase
_user_knowledge_bases: dict[int, VectorKnowledgeBase] = {}


def _get_store_root() -> str:
    """向量库根目录（每次读取环境变量，便于测试覆盖）"""
    return os.getenv("VECTOR_STORE_DIR", _DEFAULT_STORE_DIR)


def _get_shared_knowledge_base() -> VectorKnowledgeBase:
    """获取共享知识库实例（按根路径缓存，环境变量变化时自动重建）"""
    root = _get_store_root()
    if root not in _shared_knowledge_bases:
        _shared_knowledge_bases[root] = VectorKnowledgeBase(root)
    return _shared_knowledge_bases[root]


def _get_user_knowledge_base(user_id: int) -> VectorKnowledgeBase:
    """获取（并缓存）某个用户的私有知识库实例"""
    cache_key = (user_id, _get_store_root())
    if cache_key not in _user_knowledge_bases:
        user_root = os.path.join(_get_store_root(), f"user_{user_id}")
        _user_knowledge_bases[cache_key] = VectorKnowledgeBase(user_root)
    return _user_knowledge_bases[cache_key]


def _resolve_knowledge_bases() -> list[VectorKnowledgeBase]:
    """
    解析当前请求可见的知识库列表（私有优先，共享兜底）

    :return: VectorKnowledgeBase 列表；无用户上下文时只返回共享库
    """
    user_id = get_user_context()
    if user_id is None:
        return [_get_shared_knowledge_base()]
    return [_get_user_knowledge_base(user_id), _get_shared_knowledge_base()]


def _list_all_collections() -> list[CollectionInfo]:
    """合并用户私有库与共享库的集合列表（重名时私有优先）"""
    seen: set[str] = set()
    collections: list[CollectionInfo] = []
    for knowledge_base in _resolve_knowledge_bases():
        for info in knowledge_base.list_collections():
            if info.name in seen:
                continue
            seen.add(info.name)
            collections.append(info)
    return collections


def _find_collection_owner(name: str) -> Optional[VectorKnowledgeBase]:
    """按集合名找到承载它的知识库（私有库优先）"""
    for knowledge_base in _resolve_knowledge_bases():
        if knowledge_base.get_collection(name) is not None:
            return knowledge_base
    return None


@tool
def list_knowledge_collections() -> str:
    """
    列出本地向量知识库中所有可用的集合及其包含的文件

    作用：让模型先了解“哪个集合能回答哪类内部文档问题”，再决定后续检索哪个集合。
    调用 search_knowledge_base 之前，应先调用本工具确认集合名称。
    :return: 集合名称、分片数量、包含文件等路由信息；无集合时返回中文提示
    """
    monitor.report_tool(tool_name="知识库集合列表查询工具：list_knowledge_collections")

    try:
        collections = _list_all_collections()
    except Exception as e:
        logger.warning("知识库集合列表查询失败: %s", e)
        return f"查询知识库集合失败: {e}"

    if not collections:
        return (
            "当前没有任何可用的知识库集合。如需使用私有知识库，请先运行 app.rag.ingest 构建向量库。"
        )

    return "可用的知识库集合如下：\n" + "\n".join(info.to_prompt_text() for info in collections)


@tool
def search_knowledge_base(
    question: str,
    collection: str,
    top_k: int = 4,
) -> str:
    """
    在指定知识库集合中检索与问题最相关的文档片段

    注意：调用此工具之前，必须先调用 list_knowledge_collections 确认集合名称。
    :param question: 要检索的问题或关键词
    :param collection: 集合名称，必须来自 list_knowledge_collections 的返回结果
    :param top_k: 返回的最大文档分片数，默认 4
    :return: 相关文档分片文本及其来源文件名；异常时返回中文错误提示
    """
    monitor.report_tool(
        tool_name="知识库检索工具：search_knowledge_base",
        args={"question": question, "collection": collection, "top_k": top_k},
    )

    owner = _find_collection_owner(collection)
    if owner is None:
        available = [info.name for info in _list_all_collections()]
        return f"检索失败: 知识库集合 '{collection}' 不存在（可用集合: {available}）"

    try:
        results = owner.search(collection, question, top_k=top_k)
    except FileNotFoundError as e:
        return f"检索失败: {e}"
    except Exception as e:
        logger.warning("知识库检索失败: %s", e)
        return f"检索失败，错误原因: {e}"

    if not results:
        return f"集合 '{collection}' 中没有找到与问题相关的内容。"

    # 每条结果都带上来源文件名和距离分数，方便主智能体引用出处
    sections = []
    for index, (doc, score) in enumerate(results, start=1):
        source = doc.metadata.get("source", "未知来源")
        sections.append(f"[片段{index} | 来源: {source} | 相关度: {score:.4f}]\n{doc.page_content}")
    return "\n\n".join(sections)


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证向量库目录配置和集合列表
    print(list_knowledge_collections.invoke({}))
