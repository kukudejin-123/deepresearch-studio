import { API_BASE_URL } from "./config";

const TOKEN_STORAGE_KEY = "deepresearch-studio.token";
const USER_STORAGE_KEY = "deepresearch-studio.user";

export interface AuthUser {
  id: number;
  username: string;
}

export interface AuthResult {
  token: string;
  user: AuthUser;
}

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 隐私模式等场景下写入失败时退化为内存态，不影响本次会话使用
  }
}

function removeStorage(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // 忽略清理失败
  }
}

export function getToken(): string | null {
  return readStorage(TOKEN_STORAGE_KEY);
}

export function getStoredUser(): AuthUser | null {
  const raw = readStorage(USER_STORAGE_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function saveAuth(result: AuthResult): void {
  writeStorage(TOKEN_STORAGE_KEY, result.token);
  writeStorage(USER_STORAGE_KEY, JSON.stringify(result.user));
}

export function clearAuth(): void {
  removeStorage(TOKEN_STORAGE_KEY);
  removeStorage(USER_STORAGE_KEY);
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

/** 带认证的 JSON 请求：401 时清理本地登录态并跳转登录 */
export async function authFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  Object.entries(authHeaders()).forEach(([key, value]) => headers.set(key, value));

  const response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    if (response.status === 401) {
      clearAuth();
    }
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

export async function registerUser(username: string, password: string): Promise<AuthResult> {
  return authFetch<AuthResult>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
}

export async function loginUser(username: string, password: string): Promise<AuthResult> {
  return authFetch<AuthResult>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
}

/** 退出登录：清理本地态，交由界面切回登录页 */
export function logout(): void {
  clearAuth();
}
