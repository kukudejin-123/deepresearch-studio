"""
FastAPI 接口层与项目闭环入口

负责承接前端的用户注册登录、任务提交、任务取消、任务查询、文件上传/下载、
输出文件列表查询和 WebSocket 长连接。HTTP 接口只做轻量调度，真正的
DeepAgents 执行放到后台任务中；执行进度、工具调用和最终结果由 monitor
按 thread_id 推送给前端。任务状态同时写入 SQLite 任务存储，
服务重启后仍可查询历史。

安全模型（第一期改造）：
- 除注册/登录外，所有接口都需要 JWT 认证（AUTH_ENABLED=false 时退化为开发用户）；
- 任务、文件、上传目录全部按 user_id 隔离，接口层校验资源归属；
- 任务提交按用户限流，登录按 IP+用户名限流；
- 上传文件经过类型白名单、大小上限和文件名清洗；
- WebSocket 通过 query token 认证，并校验 thread_id 归属。
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.agent.checkpointer import close_checkpointer, get_checkpointer
from app.agent.main_agent import task_store
from app.api.auth import (
    DEV_USER,
    auth_enabled,
    bearer_scheme,
    create_access_token,
    decode_access_token,
    get_current_user,
    get_user_store,
    validate_registration,
)
from app.api.metrics import (
    MetricsMiddleware,
    record_task_cancelled,
    record_task_submitted,
    render_metrics,
)
from app.api.monitor import manager
from app.api.rate_limit import login_rate_limiter, task_rate_limiter
from app.api.task_store import STATUS_CANCELLED, STATUS_FAILED
from app.api.upload_guard import (
    UploadTooLargeError,
    get_max_upload_bytes,
    sanitize_filename,
    validate_upload,
)
from app.events import get_event_store
from app.queue import get_task_queue
from app.utils.logging_config import get_logger, setup_logging

logger = get_logger(__name__)

# 单次请求允许的最大上传文件数
MAX_UPLOAD_FILES = 10


def _max_concurrent_tasks() -> int:
    """单用户并发任务上限（可用 MAX_CONCURRENT_TASKS_PER_USER 覆盖）"""
    try:
        return max(1, int(os.getenv("MAX_CONCURRENT_TASKS_PER_USER", "2")))
    except ValueError:
        return 2


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    服务生命周期入口。

    启动时：初始化日志、绑定事件循环到 WebSocket 管理器、预热检查点表、
    启动 Redis 事件桥订阅（arq 队列模式下把 worker 的事件转发给 WebSocket）；
    停机时：关闭事件桥与检查点连接。
    """
    setup_logging("INFO")
    loop = asyncio.get_running_loop()
    manager.set_loop(loop)
    # 预热检查点：启动即建表，避免首个任务请求时才感知数据库问题
    await get_checkpointer()
    # 预热事件存储与任务队列，让启动日志暴露配置问题
    get_event_store()
    queue = get_task_queue()
    _start_event_bridge()
    logger.info(
        "DeepResearch Studio 服务启动完成（认证%s，队列 %s）",
        "已启用" if auth_enabled() else "未启用，开发模式",
        queue.queue_type,
    )
    yield
    await _stop_event_bridge()
    await close_checkpointer()
    logger.info("DeepResearch Studio 服务已停机")


def _start_event_bridge() -> None:
    """
    启动 Redis 事件桥订阅（仅 arq 模式）

    订阅活跃 thread 的频道，把 worker 进程产生的 monitor 事件转发给
    本进程的 WebSocket 连接；memory 模式下无需跨进程桥。
    """
    from app.events import get_event_bridge

    bridge = get_event_bridge()
    if bridge is None:
        return

    async def handle_event(thread_id: str, payload: dict) -> None:
        websocket = manager.active_connections.get(thread_id)
        if websocket is not None:
            try:
                await websocket.send_json(payload)
            except Exception as e:
                logger.warning("事件桥转发失败: thread_id=%s, error=%s", thread_id, e)

    # 订阅范围动态取自 WebSocket 连接注册表：有客户端连接的 thread 才订阅
    bridge.start_subscriber(
        thread_ids_provider=lambda: list(manager.active_connections.keys()),
        handler=handle_event,
    )


async def _stop_event_bridge() -> None:
    """关闭 Redis 事件桥"""
    from app.events import get_event_bridge

    bridge = get_event_bridge()
    if bridge is not None:
        await bridge.close()


# 当前文件位于 app/api/server.py，运行时目录统一收敛到 app 目录
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent

app = FastAPI(title="DeepResearch Studio API", lifespan=lifespan)

# 接口指标中间件（第二期可观测性）
app.add_middleware(MetricsMiddleware)

# output 保存每个会话最终工作区，前端只允许从这里浏览和下载生成文件
output_dir = project_root / "output"
output_dir.mkdir(exist_ok=True)

# updated 暂存用户上传文件，run_deep_agent 启动时会复制到对应 output 工作目录
updated_dir = project_root / "updated"
updated_dir.mkdir(exist_ok=True)

# 教学项目通常前后端分别本地启动，这里放开跨域以便 Vite 页面直接调用 API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class TaskRequest(BaseModel):
    """前端启动任务时提交的请求体。"""

    query: str
    thread_id: str = None


class RegisterRequest(BaseModel):
    """注册请求体。"""

    username: str
    password: str


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


# ---------------------------------------------------------------------------
# 用户认证接口
# ---------------------------------------------------------------------------


@app.post("/api/auth/register", status_code=201)
async def register(request: RegisterRequest):
    """
    注册新用户并直接返回登录令牌

    :param request: 用户名和密码
    :return: JWT 令牌与用户信息
    """
    invalid_reason = validate_registration(request.username, request.password)
    if invalid_reason:
        raise HTTPException(status_code=400, detail=invalid_reason)

    try:
        user = get_user_store().create_user(request.username, request.password)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return {
        "token": create_access_token(user["id"], user["username"]),
        "user": user,
    }


@app.post("/api/auth/login")
async def login(request: LoginRequest, http_request: Request):
    """
    登录并签发 JWT

    按 IP + 用户名限流，缓解暴力破解；失败时统一返回相同提示，不暴露
    “用户不存在”和“密码错误”的区别。
    """
    client_ip = http_request.client.host if http_request.client else "unknown"
    allowed, retry_after = login_rate_limiter.check(f"login:{client_ip}:{request.username}")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {int(retry_after) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    user = get_user_store().verify_user(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    return {
        "token": create_access_token(user["id"], user["username"]),
        "user": user,
    }


@app.get("/api/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    """
    查询当前登录用户信息

    :return: 用户 ID 和用户名
    """
    return {"user": current_user}


# ---------------------------------------------------------------------------
# 资源隔离辅助函数
# ---------------------------------------------------------------------------


def _user_output_root(user: dict) -> Path:
    """当前用户的输出根目录：output/user_{user_id}"""
    return output_dir / f"user_{user['id']}"


def _user_updated_root(user: dict) -> Path:
    """当前用户的上传暂存根目录：updated/user_{user_id}"""
    return updated_dir / f"user_{user['id']}"


def _resolve_within(path: str, root: Path) -> tuple[Optional[Path], Optional[str]]:
    """
    把请求路径解析到指定根目录内

    :param path: 请求传入的路径
    :param root: 允许访问的根目录
    :return: (绝对路径, 错误信息)；越界或非法时绝对路径为 None
    """
    try:
        abs_path = Path(path).resolve()
        root_abs = root.resolve()
    except Exception:
        return None, "无效的路径参数"

    if not abs_path.is_relative_to(root_abs):
        return None, "拒绝访问: 只能访问当前用户名下的文件"
    return abs_path, None


def _get_owned_task(thread_id: str, user: dict) -> dict:
    """
    查询任务并校验归属

    其他用户的任务一律按 404 返回，避免暴露资源是否存在。
    """
    task = task_store.get(thread_id)
    if task is None or task.get("user_id") != user["id"]:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


# ---------------------------------------------------------------------------
# 任务接口
# ---------------------------------------------------------------------------


@app.post("/api/task")
async def run_task(
    request: TaskRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_user),
):
    """
    启动一次 DeepAgents 后台任务。

    HTTP 请求只负责把任务提交到队列并立即返回，后续执行轨迹、子智能体调用和最终
    答案都会由 monitor 通过 `/ws/{thread_id}` 推送给同一会话的前端。
    任务登记信息写入 SQLite，可通过 /api/tasks 查询。

    队列模式（第二期）：
    - memory（默认）：任务在 API 进程内以后台协程执行；
    - arq：任务入队 Redis，由独立 worker 进程执行，API 重启不丢任务。

    限流：每个用户 TASK_RATE_LIMIT 次 / TASK_RATE_WINDOW_SECONDS 秒，
    超限返回 429，防止 LLM 费用被打爆。
    """
    allowed, retry_after = task_rate_limiter.check(f"task:{current_user['id']}")
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"任务提交过于频繁，请 {int(retry_after) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    # 单用户并发上限（第三期）：防止一个用户占满 worker / 打爆 LLM 配额。
    # 计数来自共享 TaskStore，memory 与 arq 两种队列模式下都生效。
    max_concurrent = _max_concurrent_tasks()
    if task_store.count_running(user_id=current_user["id"]) >= max_concurrent:
        raise HTTPException(
            status_code=429,
            detail=f"同时进行的任务数已达上限（{max_concurrent}），请等待现有任务完成",
        )

    thread_id = request.thread_id or str(uuid.uuid4())

    # 任务与用户绑定，后续查询/取消都做归属校验
    task_store.create(thread_id, request.query, user_id=current_user["id"])

    try:
        submission = await get_task_queue().submit(
            query=request.query,
            thread_id=thread_id,
            user_id=current_user["id"],
        )
    except Exception as e:
        # 入队失败（如 Redis 不可用）：任务标记失败，不留下“ running 僵尸任务”
        task_store.set_status(thread_id, STATUS_FAILED, error=f"任务入队失败: {e}")
        logger.error("任务入队失败: thread_id=%s, error=%s", thread_id, e)
        raise HTTPException(status_code=503, detail=f"任务队列暂不可用: {e}") from e

    record_task_submitted()
    logger.info("任务已提交: thread_id=%s, 队列=%s", thread_id, submission.queue_type)
    return {"status": "started", "thread_id": thread_id, "queue": submission.queue_type}


@app.post("/api/task/{thread_id}/cancel")
async def cancel_task(thread_id: str, current_user: dict = Depends(get_current_user)):
    """
    取消指定 thread_id 对应的后台任务。

    两种队列模式的取消语义：
    - memory：直接取消进程内 asyncio 任务；
    - arq：队列中未执行的任务由 arq abort；已开始执行的任务走协作式取消——
      先把状态置为 cancelled，worker 在流式执行中定期检查并主动收尾。
    """
    # 先校验任务归属，再执行取消
    _get_owned_task(thread_id, current_user)

    queue = get_task_queue()
    result = await queue.cancel(thread_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail="任务不存在或已结束")

    if result == "cancelled":
        # 队列侧已中止：同步任务状态，保证列表/详情一致
        task_store.set_status(thread_id, STATUS_CANCELLED)
        record_task_cancelled()
        return {"status": "cancelled", "thread_id": thread_id}

    # cancelling：运行中任务无法强杀，置位取消标记等 worker 协作收尾
    task_store.set_status(thread_id, STATUS_CANCELLED)
    record_task_cancelled()
    return {"status": "cancelling", "thread_id": thread_id}


@app.get("/api/tasks")
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """
    查询当前用户最近的任务列表（含状态和时间）

    任务状态来自 SQLite 任务存储，服务重启后历史记录依然可查；
    列表按用户隔离，看不到其他用户的任务。
    :param limit: 返回条数上限，默认 50
    """
    tasks = task_store.list(user_id=current_user["id"], limit=limit)
    return {"tasks": tasks}


@app.get("/api/task/{thread_id}")
async def get_task(thread_id: str, current_user: dict = Depends(get_current_user)):
    """
    查询单个任务的状态详情

    :param thread_id: 任务会话 ID
    :return: 任务记录；不存在或不属于当前用户时返回 404
    """
    return {"task": _get_owned_task(thread_id, current_user)}


@app.get("/api/task/{thread_id}/events")
async def get_task_events(
    thread_id: str,
    after_seq: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """
    查询任务的执行事件（断线回放）

    前端在 WebSocket 重连或页面刷新后，先按 after_seq 增量拉取历史事件，
    再继续接收实时推送，避免执行过程“断档”。

    :param thread_id: 任务会话 ID
    :param after_seq: 只返回 seq 大于该值的事件（前端记录已收到的最大 seq）
    :return: 事件列表与当前最大 seq
    """
    _get_owned_task(thread_id, current_user)

    store = get_event_store()
    events = store.list(thread_id, after_seq=after_seq)
    return {"events": events, "last_seq": store.last_seq(thread_id)}


@app.get("/metrics")
async def metrics():
    """
    Prometheus 指标端点（第二期可观测性）

    暴露任务计数、活跃任务数、工具调用、接口请求与任务耗时分布；
    采集配置示例见 README「可观测性」一节。
    """
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


@app.get("/api/health")
async def health():
    """
    健康检查端点（第三期容器化）

    供 Docker healthcheck / 负载均衡探活使用：检查进程存活与关键存储
    （任务库）可读写。不触发 LLM、MySQL 等外部依赖，避免探活误判。
    """
    checks: dict[str, str] = {"status": "ok"}
    try:
        # 轻量探测任务存储可读写（建表语句是幂等的）
        task_store.list(limit=1)
        checks["task_store"] = "ok"
    except Exception as e:
        checks["task_store"] = f"error: {e}"
        checks["status"] = "degraded"

    status_code = 200 if checks["status"] == "ok" else 503
    return JSONResponse(status_code=status_code, content=checks)


# ---------------------------------------------------------------------------
# 文件上传 / 下载 / 列表
# ---------------------------------------------------------------------------


@app.post("/api/upload")
async def upload_files(
    files: List[UploadFile] = File(...),
    thread_id: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """
    文件上传接口 (File Upload)。

    目标：
    1. 接收用户上传的一个或多个文件；
    2. 经过类型白名单、大小上限和文件名清洗后，保存到
       `updated/user_{user_id}/session_{thread_id}` 目录；
    3. 供 Agent 在后续任务中读取和分析。

    Args:
        files (List[UploadFile]): 文件对象列表。
        thread_id (str): 关联的任务会话 ID。
    """
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {MAX_UPLOAD_FILES} 个文件",
        )

    # 上传文件按用户 + 会话隔离保存，避免不同用户读取到彼此的附件
    target_dir = _user_updated_root(current_user) / f"session_{thread_id}"
    target_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = get_max_upload_bytes()
    saved_files: list[str] = []

    for file in files:
        # 先做静态校验：文件名合法 + 扩展名在白名单
        invalid_reason = validate_upload(file.filename or "")
        if invalid_reason:
            raise HTTPException(status_code=400, detail=invalid_reason)

        safe_name = sanitize_filename(file.filename)
        file_path = target_dir / safe_name

        # 流式写入时实时统计字节数，超限立即中断并删除半成品
        written = 0
        try:
            with file_path.open("wb") as buffer:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise UploadTooLargeError(safe_name, max_bytes)
                    buffer.write(chunk)
        except UploadTooLargeError as e:
            # with 块退出时文件已关闭，可以安全删除半成品
            file_path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail=str(e)) from e
        except Exception as e:
            file_path.unlink(missing_ok=True)
            logger.warning("文件写入失败: %s (%s)", safe_name, e)
            raise HTTPException(status_code=500, detail=f"文件保存失败: {e}") from e

        saved_files.append(safe_name)

    logger.info(
        "文件上传完成: user_id=%s, thread_id=%s, files=%s",
        current_user["id"],
        thread_id,
        saved_files,
    )
    return {"status": "uploaded", "files": saved_files}


@app.get("/api/download")
async def download_file(
    path: str,
    token: Optional[str] = Query(default=None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    """
    文件下载接口 (File Download)。

    目标：
    1. 根据绝对路径下载文件；
    2. 严格的安全检查：路径必须位于当前用户的输出目录内，防止越权访问。

    认证方式：优先 Authorization 头；浏览器 <a href> 直接下载无法携带请求头，
    因此同时接受 token 查询参数（该方式的取舍已在文档中说明）。
    """
    current_user = await _resolve_download_user(token, credentials)

    abs_path, error = _resolve_within(path, _user_output_root(current_user))
    if abs_path is None:
        # 越权访问返回 403（而不是 200 + 错误体），便于浏览器与监控识别失败
        logger.warning("下载路径越权，已拒绝: user_id=%s, path=%s", current_user["id"], path)
        raise HTTPException(status_code=403, detail=error)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")

    # FileResponse 会以流式响应返回文件内容，并让浏览器使用原文件名下载
    return FileResponse(abs_path, filename=abs_path.name)


async def _resolve_download_user(
    token: Optional[str],
    credentials: Optional[HTTPAuthorizationCredentials],
) -> dict:
    """
    解析下载接口的当前用户（请求头或查询参数二选一）

    :param token: 查询参数中的 JWT
    :param credentials: Authorization 头中的 Bearer 凭证
    :return: 用户信息字典
    """
    if not auth_enabled():
        return dict(DEV_USER)

    raw_token = token or (credentials.credentials if credentials else None)
    if not raw_token:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")

    payload = decode_access_token(raw_token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录凭证无效或已过期")

    user = get_user_store().get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    return {"id": user["id"], "username": user["username"]}


@app.get("/api/files")
async def list_files(path: str, current_user: dict = Depends(get_current_user)):
    """
    文件列表查询接口 (File Explorer)。

    目标：
    1. 列出指定目录下的所有生成文件；
    2. 提供文件元数据（大小、修改时间、下载所需路径）；
    3. 严格的安全检查：只能查看当前用户输出目录内的内容。

    Args:
        path (str): 目标目录的绝对路径 (必须在当前用户的 output 目录下)。
    """
    abs_path, error = _resolve_within(path, _user_output_root(current_user))
    if abs_path is None:
        logger.warning("文件列表路径越权，已拒绝: user_id=%s, path=%s", current_user["id"], path)
        raise HTTPException(status_code=403, detail=error)

    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="目录不存在")

    files = []
    try:
        # 递归返回文件元数据，前端据此渲染文件列表并发起下载请求
        for file_path in abs_path.rglob("*"):
            if file_path.is_file():
                stat = file_path.stat()
                files.append(
                    {
                        "name": file_path.name,
                        "type": "file",
                        "path": str(file_path),
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # 最新生成的文件排在前面，方便用户优先看到本次任务产物
    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return {"files": files}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(websocket: WebSocket, thread_id: str):
    """
    WebSocket 实时通讯核心接口 (Real-time Communication)。

    认证方式：连接 URL 携带 token 查询参数（浏览器 WebSocket 不支持自定义
    请求头），服务端校验 JWT。

    归属校验语义（第二期修正）：
    - 任务已存在且属于其他用户 -> 拒绝连接（403），防止越权订阅执行事件；
    - 任务不存在 -> 允许连接。这是前端的正常工作流：客户端预生成 thread_id、
      先建立 WebSocket 再提交任务；thread_id 为 UUID 且按用户本地存储，
      猜测他人 thread_id 的可行性可忽略。
    """
    current_user = await _authenticate_websocket(websocket)
    if current_user is None:
        return

    task = task_store.get(thread_id)
    if task is not None and task.get("user_id") != current_user["id"]:
        await websocket.close(code=4404, reason="任务不存在或不属于当前用户")
        logger.info("WebSocket 归属校验失败: thread_id=%s", thread_id)
        return

    logger.info("WebSocket 连接请求: thread_id=%s, user_id=%s", thread_id, current_user["id"])

    # 连接建立后立即按 thread_id 注册，monitor 后续才能把事件定向推给当前页面
    await manager.connect(websocket, thread_id)

    try:
        while True:
            # 前端通常发送 ping 心跳；服务端回复 pong，顺便维持连接活跃
            data = await websocket.receive_text()
            await websocket.send_json({"type": "pong", "message": f"服务端已收到: {data}"})

    except WebSocketDisconnect:
        # 只移除当前 WebSocket 实例，避免旧连接断开时误删同 thread_id 的新连接
        manager.disconnect(websocket, thread_id)
        logger.info("WebSocket 客户端主动断开: thread_id=%s", thread_id)

    except Exception as e:
        logger.warning("WebSocket 连接异常: thread_id=%s, error=%s", thread_id, e)
        manager.disconnect(websocket, thread_id)


async def _authenticate_websocket(websocket: WebSocket) -> Optional[dict]:
    """
    校验 WebSocket 连接的 token 查询参数

    认证失败时直接关闭连接（拒绝握手）；成功时不调用 accept——
    accept 由后续 manager.connect 统一执行，避免重复 accept 报协议错误。

    :return: 用户信息字典；认证失败时已关闭连接并返回 None
    """
    if not auth_enabled():
        return dict(DEV_USER)

    token = websocket.query_params.get("token")
    payload = decode_access_token(token) if token else None
    if payload is None:
        await websocket.close(code=4401, reason="未登录或登录已过期")
        return None

    user = get_user_store().get_user(int(payload["sub"]))
    if user is None:
        await websocket.close(code=4401, reason="用户不存在或已被删除")
        return None

    return {"id": user["id"], "username": user["username"]}


if __name__ == "__main__":
    setup_logging("INFO")
    uvicorn.run("app.api.server:app", host="0.0.0.0", port=8000, reload=True)
