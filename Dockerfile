# syntax=docker/dockerfile:1
#
# DeepResearch Studio 后端镜像（API + worker 共用）
#
# 多阶段构建：构建阶段用 uv 安装依赖，运行阶段只保留虚拟环境与源码，
# 以非 root 用户运行，内置健康检查。

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

# 从官方镜像复制 uv 二进制，避免在镜像内联网安装
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

# 先拷贝依赖清单：依赖不变时命中 Docker 层缓存，跳过重复安装
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# 再拷贝源码（源码变动不影响依赖层）
COPY app ./app

# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="deepresearch-studio-api" \
      org.opencontainers.image.description="DeepResearch Studio backend (API / arq worker)" \
      org.opencontainers.image.source="https://github.com/kukudejin-123/deepresearch-studio"

# 非 root 用户运行
RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app ./app

# 运行时目录：数据（检查点/事件/向量库）、产物、上传暂存
RUN mkdir -p /app/app/data /app/app/output /app/app/updated \
    && chown -R app:app /app

USER app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# 健康检查：/api/health 不依赖 LLM / MySQL 等外部服务，避免探活误判
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# 默认启动 API 服务；compose 中 worker 服务用 command 覆盖为 arq worker
CMD ["uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
