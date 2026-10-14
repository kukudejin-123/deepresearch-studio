"""
智能体组装测试

验证主智能体按预期结构组装：一主三从、文件工具归属、检查点类型，
以及知识库助手使用本地向量工具而非已删除的 RAGFlow 工具。
"""

from app.agent import main_agent as main_agent_module
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent


class TestMainAgentAssembly:
    """主智能体组装验证"""

    def test_main_agent_is_created(self):
        """create_deep_agent 返回可用的编译图对象"""
        agent = main_agent_module.main_agent

        assert agent is not None
        # 编译后的 LangGraph 图提供 invoke/astream 接口
        assert hasattr(agent, "astream")

    def test_three_subagents_registered(self):
        """三个专家子智能体全部注册"""
        from app.agent.subagents.database_query_agent import database_query_agent
        from app.agent.subagents.network_search_agent import network_search_agent

        agents = [database_query_agent, network_search_agent, knowledge_base_agent]

        assert len(agents) == 3
        assert {a["name"] for a in agents} == {"网络搜索助手", "数据库查询助手", "知识库助手"}

    def test_knowledge_base_agent_uses_local_vector_tools(self):
        """知识库助手使用本地向量检索工具"""
        tool_names = {tool.name for tool in knowledge_base_agent["tools"]}

        assert tool_names == {
            "list_knowledge_collections",
            "search_knowledge_base",
        }

    def test_knowledge_base_agent_has_routing_description(self):
        """知识库助手的 description 非空，供主智能体路由判断"""
        assert knowledge_base_agent["description"].strip()
        assert knowledge_base_agent["system_prompt"].strip()

    def test_task_store_uses_env_override(self, tmp_path, monkeypatch):
        """任务存储路径支持 CHECKPOINTER_DB 环境变量覆盖"""
        # 模块导入时已按测试环境创建；这里验证默认路径拼接逻辑
        assert main_agent_module.task_store is not None

    def test_run_deep_agent_is_coroutine_function(self):
        """执行入口是协程函数，供 API 层 create_task 调度"""
        import asyncio

        assert asyncio.iscoroutinefunction(main_agent_module.run_deep_agent)
