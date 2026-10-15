# syntax=docker/dockerfile:1
#
# DeepResearch Studio 前端镜像
#
# 多阶段构建：Node 阶段安装依赖并构建产物，Nginx 阶段只保留静态文件
# 与反向代理配置（同源部署：/api、/ws、/metrics 代理到后端容器，
# 免去跨域与多端口暴露）。

# ---------- 构建阶段 ----------
FROM node:20-alpine AS builder

RUN corepack enable

WORKDIR /build

# 先装依赖（锁文件不变时命中缓存）
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# 拷贝源码并构建
COPY . .

# 构建参数：同源部署传空串（默认），分离部署传后端地址
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN pnpm build

# ---------- 运行阶段 ----------
FROM nginx:1.27-alpine AS runtime

LABEL org.opencontainers.image.title="deepresearch-studio-frontend" \
      org.opencontainers.image.description="DeepResearch Studio frontend (React + Vite + Nginx)" \
      org.opencontainers.image.source="https://github.com/kukudejin-123/deepresearch-studio"

# SPA 路由 + API/WebSocket 反向代理配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
