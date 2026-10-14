"""
本地向量知识库工具模块

封装两个给知识库子智能体使用的 LangChain 工具：
list_knowledge_collections 用于发现本地 FAISS 向量库中有哪些集合，
search_knowledge_base 用于在指定集合中检索与问题相关的文档分片。

替代原 RAGFlow 外部服务后，私有知识库问答完全在项目内闭环。
"""

import os
from typing import Optional

from langchain_core.tools import tool

from app.api.monitor import monitor
from app.rag.vector_store import VectorKnowledgeBase
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 当前文件位于 app/tools/vector_store_tools.py，parents[1] 即 app 目录
_DEFAULT_STORE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "vector_store",
)

# 模块级复用知识库管理器；目录不存在时列表查询返回空，不抛异常
knowledge_base = VectorKnowledgeBase(os.getenv("VECTOR_STORE_DIR", _DEFAULT_STORE_DIR))


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
        collections = knowledge_base.list_collections()
    except Exception as e:
        logger.warning("知识库集合列表查询失败: %s", e)
        return f"查询知识库集合失败: {e}"

    if not collections:
        return "当前没有任何可用的知识库集合。如需使用私有知识库，请先运行 app.rag.ingest 构建向量库。"

    return "可用的知识库集合如下：\n" + "\n".join(
        info.to_prompt_text() for info in collections
    )


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

    try:
        results = knowledge_base.search(collection, question, top_k=top_k)
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
        sections.append(
            f"[片段{index} | 来源: {source} | 相关度: {score:.4f}]\n{doc.page_content}"
        )
    return "\n\n".join(sections)


def get_default_collection() -> Optional[str]:
    """
    获取默认集合名称（集合唯一时直接返回，便于减少一次工具调用）

    :return: 集合名称；无集合或有多个集合时返回 None
    """
    collections = knowledge_base.list_collections()
    if len(collections) == 1:
        return collections[0].name
    return None


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证向量库目录配置和集合列表
    print(list_knowledge_collections.invoke({}))
