"""
知识库子智能体配置模块

将 app/prompt/prompts.yml 中的 knowledge_base 配置与本地向量检索工具组装成
DeepAgents 可识别的字典式子智能体。主智能体后续会根据 description
决定是否把企业内部非结构化文档查询任务分派给它。
"""

from app.agent.prompts import sub_agents_content
from app.tools.vector_store_tools import (
    list_knowledge_collections,
    search_knowledge_base,
)

# 知识库助手处理内部非结构化文档，与网络搜索助手、数据库查询助手形成互补
# 它遵循“先查集合列表 -> 再向指定集合检索”的工作顺序
# tools 列表声明该子智能体可以发现集合，并在集合内做语义检索
knowledge_base_agent = {
    "name": sub_agents_content["knowledge_base"]["name"],
    "description": sub_agents_content["knowledge_base"]["description"],
    "system_prompt": sub_agents_content["knowledge_base"]["system_prompt"],
    "tools": [list_knowledge_collections, search_knowledge_base],
}
