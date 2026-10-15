const DEFAULT_API_BASE_URL = "http://localhost:8000";

function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

function deriveWsBaseUrl(apiBaseUrl: string): string {
  if (import.meta.env.VITE_WS_BASE_URL) {
    return stripTrailingSlash(import.meta.env.VITE_WS_BASE_URL);
  }

  // 空基地址 = 同源部署：WebSocket 走当前页面的协议与主机
  if (!apiBaseUrl) {
    return `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
  }

  if (apiBaseUrl.startsWith("https://")) {
    return apiBaseUrl.replace(/^https:\/\//, "wss://");
  }

  if (apiBaseUrl.startsWith("http://")) {
    return apiBaseUrl.replace(/^http:\/\//, "ws://");
  }

  return `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;
}

/**
 * 解析 API 基地址：
 * - 环境变量未设置：本地开发默认 http://localhost:8000；
 * - 环境变量为空字符串：同源部署（容器内经 Nginx 反代 /api、/ws）；
 * - 环境变量有值：显式指定（前后端分离部署）。
 */
function resolveApiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL;
  if (configured === undefined) {
    return DEFAULT_API_BASE_URL;
  }
  return stripTrailingSlash(configured);
}

export const API_BASE_URL = resolveApiBaseUrl();

export const WS_BASE_URL = deriveWsBaseUrl(API_BASE_URL);
