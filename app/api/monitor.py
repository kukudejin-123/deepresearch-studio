"""
Agent 执行过程监控模块

负责把工具调用、子智能体调用、任务结果和会话目录等事件统一包装后推送给前端
在 Web 服务中优先通过 WebSocket 定向推送；在脚本调试场景中保留结构化日志输出
"""

import asyncio
import builtins
import datetime
from typing import Any, Optional

from fastapi import WebSocket

from app.api.context import get_thread_context
from app.utils.logging_config import get_logger

logger = get_logger(__name__)


class ToolMonitor:
    """
    工具和助手调用的统一监控入口

    业务工具只需要导入全局 monitor，并调用 report_tool/report_assistant 等方法
    具体是通过 WebSocket 推送，还是输出日志，由本类内部统一处理
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ToolMonitor, cls).__new__(cls)
            cls._instance.websocket_manager = None
        return cls._instance

    def set_websocket_manager(self, manager: "ConnectionManager") -> None:
        """绑定 FastAPI WebSocket 连接管理器"""
        self.websocket_manager = manager

    def _emit(
        self,
        event_type: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        构造统一监控事件，并按当前进程角色分发

        分发策略（第二期改造）：
        1. 事件落库（EventStore）：断线重连/页面刷新后按 seq 回放；
        2. Redis 事件桥：任务在 arq worker 进程执行时，事件经 pub/sub
           送到 API 进程，再推给 WebSocket；
        3. 进程内 WebSocket 管理器：单机 memory 队列模式下直接推送；
        4. 日志保底输出。

        :param event_type: 事件类型，例如 tool_start、assistant_call
        :param message: 面向前端展示的事件说明
        :param data: 附加结构化数据
        """
        payload = {
            "type": "monitor_event",
            "event": event_type,
            "message": message,
            "data": data or {},
            "timestamp": datetime.datetime.now().isoformat(),
        }

        thread_id = get_thread_context()

        # 1. 持久化：先落库再分发，保证回放数据不依赖实时推送是否成功；
        #    返回的 seq 写入 payload，前端据此对“回放事件”和“实时事件”去重
        seq = self._persist_event(thread_id, event_type, message, payload["data"])
        if seq is not None:
            payload["seq"] = seq

        # 2. 跨进程桥：worker 进程没有 WebSocket 管理器，事件走 Redis
        if self.websocket_manager is None:
            self._publish_to_bridge(thread_id, payload)

        # 3. 进程内 WebSocket 推送（API 进程或单机模式）
        if self.websocket_manager:
            try:
                manager_loop = self.websocket_manager.loop

                if manager_loop and thread_id:
                    self._send_to_websocket(payload, thread_id, manager_loop)
            except Exception as e:
                logger.warning("WebSocket 推送失败: %s", e)

        # DeepAgents 脚本调试时，如果运行时暴露了 stream_writer，也同步写入流式输出
        if hasattr(builtins, "runtime") and hasattr(builtins.runtime, "stream_writer"):
            try:
                builtins.runtime.stream_writer(payload)
            except Exception:
                pass

        # 日志保底输出，便于无前端场景下观察执行过程
        logger.info("[Monitor:%s] %s", event_type, message)

        # 工具调用指标（第二期可观测性）：失败不影响主流程
        if event_type == "tool_start":
            try:
                from app.api.metrics import record_tool_call

                record_tool_call(str((data or {}).get("tool_name", "unknown")))
            except Exception:
                pass

    def _persist_event(
        self,
        thread_id: Optional[str],
        event_type: str,
        message: str,
        data: dict[str, Any],
    ) -> Optional[int]:
        """
        事件落库；无 thread_id（脚本调试）时跳过

        :return: 事件 seq；落库失败或跳过时返回 None
        """
        if thread_id is None:
            return None
        try:
            from app.events import get_event_store

            return get_event_store().append(thread_id, event_type, message, data)
        except Exception as e:
            # 落库失败不影响主流程（实时推送与日志仍可用）
            logger.warning("事件落库失败: %s", e)
            return None

    def _publish_to_bridge(self, thread_id: Optional[str], payload: dict[str, Any]) -> None:
        """把事件发布到 Redis 桥（仅在配置了 Redis 且无进程内 WS 管理器时）"""
        if thread_id is None:
            return
        try:
            from app.events import get_event_bridge

            bridge = get_event_bridge()
            if bridge is not None:
                # fire-and-forget：发布失败已由桥内部降级为日志
                asyncio.get_running_loop().create_task(bridge.publish(thread_id, payload))
        except RuntimeError:
            # 没有运行中的事件循环（同步脚本场景），跳过跨进程桥
            pass
        except Exception as e:
            logger.warning("事件桥发布失败: %s", e)

    def _send_to_websocket(
        self,
        payload: dict[str, Any],
        thread_id: str,
        manager_loop: asyncio.AbstractEventLoop,
    ) -> None:
        """
        将监控事件投递到 WebSocket 所在事件循环

        FastAPI 的 WebSocket 必须在创建它的事件循环中发送消息
        如果当前代码已经在同一个循环里，直接 create_task；否则使用线程安全投递
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        coroutine = self.websocket_manager.send_to_thread(payload, thread_id)
        if current_loop and current_loop == manager_loop:
            current_loop.create_task(coroutine)
        else:
            asyncio.run_coroutine_threadsafe(coroutine, manager_loop)

    def report_tool(
        self,
        tool_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告开始执行某个工具"""
        self._emit(
            "tool_start",
            f"开始执行工具: {tool_name}",
            {"tool_name": tool_name, "args": args},
        )

    def report_assistant(
        self,
        assistant_name: str,
        args: Optional[dict[str, Any]] = None,
    ) -> None:
        """报告正在调用某个子智能体"""
        self._emit(
            "assistant_call",
            f"正在调用助手: {assistant_name}",
            {"assistant_name": assistant_name, "args": args},
        )

    def report_task_result(self, result: str) -> None:
        """报告任务最终结果"""
        self._emit("task_result", "任务执行完成", {"result": result})

    def report_task_cancelled(self) -> None:
        """报告任务已被用户取消"""
        self._emit("task_cancelled", "任务已取消")

    def report_session_dir(self, path: str) -> None:
        """报告当前任务工作目录"""
        self._emit("session_created", f"工作目录已创建: {path}", {"path": path})


monitor = ToolMonitor()


class ConnectionManager:
    """
    WebSocket 连接管理器

    active_connections 使用 thread_id 作为 key，保证监控事件只推送给对应任务的前端连接
    """

    def __init__(self) -> None:
        self.active_connections: dict[str, WebSocket] = {}
        # WebSocket 发送必须回到创建连接的事件循环，因此启动时需要显式绑定 loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定 FastAPI 主事件循环，并同步注册到 monitor"""
        self.loop = loop
        monitor.set_websocket_manager(self)
        logger.info("ConnectionManager 已绑定事件循环: %s", id(self.loop))

    async def connect(self, websocket: WebSocket, thread_id: str) -> None:
        """接受 WebSocket 连接，并按 thread_id 保存"""
        await websocket.accept()
        self.active_connections[thread_id] = websocket
        logger.info("WebSocket 客户端已连接: %s", thread_id)

    def disconnect(self, websocket: WebSocket, thread_id: str) -> None:
        """移除已经断开的 WebSocket 连接"""
        if self.active_connections.get(thread_id) is websocket:
            del self.active_connections[thread_id]
            logger.info("WebSocket 客户端已断开: %s", thread_id)
        else:
            logger.info("过期 WebSocket 断开，保留当前连接: %s", thread_id)

    async def send_personal_message(self, message: str, websocket: WebSocket) -> None:
        """向指定 WebSocket 发送纯文本消息"""
        await websocket.send_text(message)

    async def send_to_thread(self, message: dict[str, Any], thread_id: str) -> None:
        """向指定 thread_id 对应的前端连接发送 JSON 消息"""
        if thread_id in self.active_connections:
            websocket = self.active_connections[thread_id]
            await websocket.send_json(message)


manager = ConnectionManager()
