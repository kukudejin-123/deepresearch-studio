"""
主智能体组装与异步执行模块

负责把模型、主提示词、文件类工具和三个专家子智能体组装成 DeepAgent，
并提供 run_deep_agent 作为后续 API 层调用的统一入口。运行时还会为每个
session_id 创建独立工作目录，把任务状态写入 SQLite 任务存储，
并把工具调用、子智能体调用和最终结果推送给前端。
"""

import asyncio
import os
import shutil
import time
from pathlib import Path

from deepagents import create_deep_agent

from app.agent.checkpointer import get_checkpointer
from app.agent.llm import model
from app.agent.prompts import main_agent_content
from app.agent.subagents.database_query_agent import database_query_agent
from app.agent.subagents.knowledge_base_agent import knowledge_base_agent
from app.agent.subagents.network_search_agent import network_search_agent
from app.api.context import (
    reset_session_context,
    set_session_context,
    set_thread_context,
    set_user_context,
)
from app.api.monitor import monitor
from app.api.task_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    TaskStore,
)

# 文件类工具由主智能体直接掌握，负责读取上传附件和生成最终交付文档
from app.tools.markdown_tools import generate_markdown
from app.tools.pdf_tools import convert_md_to_pdf
from app.tools.upload_file_read_tool import read_file_content
from app.utils.logging_config import get_logger, setup_logging

logger = get_logger(__name__)

# 协作式取消的检查频率：每 N 个流式片段检查一次任务状态
# （astream 的每个 chunk 大致对应一个节点执行，5 个片段约一次工具调用周期）
_CANCEL_CHECK_INTERVAL = 5


def _is_cancelled(session_id: str) -> bool:
    """
    检查任务是否已被用户取消

    取消接口（跨进程）只能把状态写入 TaskStore；执行侧通过本函数感知。
    查询失败时按“未取消”处理，避免数据库抖动误杀正常任务。
    """
    try:
        task = task_store.get(session_id)
    except Exception as e:
        logger.warning("取消状态查询失败，按未取消处理: %s", e)
        return False
    return bool(task) and task.get("status") == STATUS_CANCELLED


def _record_task_outcome(outcome: str, started_at: float) -> None:
    """
    记录任务结局指标（第二期可观测性）

    注意：指标注册表是进程级的。memory 模式下提交与结局在同一进程，
    /metrics 数据完整；arq 模式下提交指标在 API 进程、结局指标在 worker
    进程，生产环境需要两个进程都接入 Prometheus（或 Pushgateway）。
    """
    duration = max(time.monotonic() - started_at, 0.0)
    try:
        from app.api.metrics import (
            record_task_cancelled,
            record_task_completed,
            record_task_failed,
        )

        if outcome == "completed":
            record_task_completed(duration)
        elif outcome == "failed":
            record_task_failed(duration)
        else:
            record_task_cancelled(duration)
    except Exception as e:
        # 指标失败绝不影响任务主流程
        logger.debug("任务指标记录失败: %s", e)


# 主智能体是调度中心：
# 1. tools 只放最终交付相关的文件工具
# 2. subagents 放网络、数据库、知识库三类信息获取助手
# 3. checkpointer 通过 thread_id 保存同一会话中的执行上下文（默认 SQLite 持久化）
#
# 采用惰性异步组装：AsyncSqliteSaver 需要在事件循环内创建，
# 模块导入时没有可用的 loop，因此推迟到首次执行任务时构建。
_main_agent = None
_agent_build_lock = asyncio.Lock()


async def get_main_agent():
    """
    获取全局复用的主智能体（首次调用时组装）

    :return: 组装完成的 DeepAgent 编译图
    """
    global _main_agent
    if _main_agent is not None:
        return _main_agent

    # 并发首个任务可能同时触发组装，用锁保证只构建一次
    async with _agent_build_lock:
        if _main_agent is None:
            _main_agent = create_deep_agent(
                model=model,
                system_prompt=main_agent_content["system_prompt"],
                tools=[generate_markdown, convert_md_to_pdf, read_file_content],
                checkpointer=await get_checkpointer(),
                subagents=[
                    database_query_agent,
                    network_search_agent,
                    knowledge_base_agent,
                ],
            )
            logger.info("主智能体组装完成")
    return _main_agent


# 当前文件位于 app/agent/main_agent.py，parents[1] 即 app 目录
project_root_path = Path(__file__).parents[1].resolve()

# 任务生命周期存储：默认与检查点数据库同目录（app/data/checkpoints.db），
# 可用 CHECKPOINTER_DB 环境变量覆盖，便于测试隔离
task_store = TaskStore(
    os.getenv("CHECKPOINTER_DB") or (project_root_path / "data" / "checkpoints.db")
)


async def run_deep_agent(task_query, session_id, user_id=None):
    """
    异步流式执行主智能体

    API 层会为每次任务传入用户问题、session_id 和 user_id。本函数负责准备
    会话目录（按用户隔离）、复制上传文件、写入 ContextVar，并在流式执行
    过程中把关键事件上报给前端。
    :param task_query: 前端提交的原始任务问题
    :param session_id: 当前任务 ID，同时用于 thread_id、输出目录和 WebSocket 定向推送
    :param user_id: 任务归属用户；匿名执行（脚本调试）时为 None
    """
    logger.info("开始执行会话，session_id=%s, user_id=%s", session_id, user_id)

    # 任务级指标计时起点（第二期可观测性）
    started_at = time.monotonic()

    # 每个用户、每个会话独立使用 output/user_{user_id}/session_{session_id}，
    # 避免不同用户、不同任务的产物互相覆盖，也让文件接口能按用户收紧边界
    if user_id is not None:
        session_dir = project_root_path / "output" / f"user_{user_id}" / f"session_{session_id}"
        updated_dir_path = (
            project_root_path / "updated" / f"user_{user_id}" / f"session_{session_id}"
        )
    else:
        session_dir = project_root_path / "output" / f"session_{session_id}"
        updated_dir_path = project_root_path / "updated" / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # 前端和工具使用绝对路径；提示词里只给模型相对路径，降低模型误用系统绝对路径的概率
    session_dir_str = str(session_dir).replace("\\", "/")
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace("\\", "/")

    # 上传文件先落在 updated/user_{user_id}/session_{session_id}，执行前复制到
    # 本次 output 工作目录；这样读文件工具和生成文件工具都只围绕同一个
    # session_dir 工作，且天然按用户隔离
    updated_info_prompt = ""
    if updated_dir_path.exists():
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        if files:
            for filename in files:
                # copy2 会保留上传文件的修改时间、权限等元数据，便于后续排查文件来源
                shutil.copy2(updated_dir_path / filename, session_dir / filename)

            # 把上传文件列表注入用户消息，提醒模型先调用 read_file_content 获取附件内容
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"    - {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # ContextVar 让深层工具无需显式传参，也能拿到当前会话目录、
    # WebSocket thread_id 和用户身份（知识库工具据此做资源隔离）
    session_dir_token = set_session_context(session_dir_str)
    session_id_token = set_thread_context(session_id)
    user_token = set_user_context(user_id)

    # 前端拿到工作目录后，可以展示本次任务生成的 Markdown/PDF 等产物
    monitor.report_session_dir(session_dir_str)

    # checkpointer 依赖 thread_id 区分会话记忆；同一 session_id 会复用同一条执行上下文
    config = {"configurable": {"thread_id": session_id}}

    # 工作环境指令是运行时动态补充的，约束模型只在当前会话目录读写文件
    path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传文件时，请直接将文件名作为 read_file_content 的 filename 参数传入，不要带目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """

    try:
        agent = await get_main_agent()
        # astream 会持续产出模型节点、工具节点和子智能体节点的状态片段
        chunk_count = 0
        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": task_query + path_instruction}]},
            config=config,
        ):
            # 协作式取消（第二期）：任务在 arq worker 进程执行时，接口进程
            # 无法直接取消它的 asyncio 任务；改为由取消接口把状态置为
            # cancelled，执行侧定期检查后主动收尾，两种队列模式通用
            chunk_count += 1
            if chunk_count % _CANCEL_CHECK_INTERVAL == 0 and _is_cancelled(session_id):
                logger.info("检测到取消标记，主动结束会话: session_id=%s", session_id)
                break

            # chunk 形如 {"model": {"messages": [...]}}，这里主要关心模型最新消息
            for node_name, state in chunk.items():
                if not state or "messages" not in state:
                    continue
                messages = state["messages"]
                if messages and isinstance(messages, list):
                    last_msg = messages[-1]
                    if node_name == "model":
                        if last_msg.tool_calls:
                            # DeepAgents 调用子智能体时，本质上会产生名为 task 的工具调用
                            for tool_call in last_msg.tool_calls:
                                if tool_call["name"] == "task":
                                    # 子智能体调用单独上报，前端可以展示“正在调用哪个专家助手”
                                    monitor.report_assistant(
                                        tool_call["args"]["subagent_type"],
                                        {"description": tool_call["args"]["description"]},
                                    )
                        elif last_msg.content:
                            # 模型没有继续调用工具时，最新文本内容就是本轮可反馈给前端的结果
                            logger.info(
                                "主智能体产出结果，session_id=%s，内容片段: %s",
                                session_id,
                                last_msg.content[:100],
                            )
                            monitor.report_task_result(last_msg.content)

        if _is_cancelled(session_id):
            # 协作式取消路径：状态已由取消接口置位，这里只补前端事件
            monitor.report_task_cancelled()
            logger.info("会话已按取消标记收尾，session_id=%s", session_id)
            _record_task_outcome("cancelled", started_at)
            return

        task_store.set_status(session_id, STATUS_COMPLETED)
        logger.info("会话执行完成，session_id=%s", session_id)
        _record_task_outcome("completed", started_at)

    except asyncio.CancelledError:
        task_store.set_status(session_id, STATUS_CANCELLED)
        monitor.report_task_cancelled()
        logger.info("会话已被取消，session_id=%s", session_id)
        _record_task_outcome("cancelled", started_at)
        raise
    except Exception as e:
        # 异步执行异常也走 monitor 和任务存储，保证前端能收到明确错误事件、历史可排查
        task_store.set_status(session_id, STATUS_FAILED, error=str(e))
        monitor._emit("error", f"执行主智能发生异常信息：{str(e)}")
        logger.exception("主智能体执行异常，session_id=%s", session_id)
        _record_task_outcome("failed", started_at)
    finally:
        # 任务结束后恢复 ContextVar，避免后续请求复用到本次会话目录或 thread_id
        reset_session_context(session_dir_token, session_id_token, user_token)


if __name__ == "__main__":
    import asyncio

    setup_logging("INFO")
    asyncio.run(run_deep_agent("从网络查询机器人信息，并生成Markdown文件", "test_session_001"))
