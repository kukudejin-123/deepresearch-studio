import { API_BASE_URL } from "./config";
import { authFetch, getToken } from "./auth";
import type {
  CancelTaskResponse,
  FileListResponse,
  TaskResponse,
  UploadResponse
} from "../types";

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

export async function startTask(query: string, threadId: string): Promise<TaskResponse> {
  return authFetch<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return authFetch<CancelTaskResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`), {
    method: "POST"
  });
}

export async function uploadSessionFiles(
  files: File[],
  threadId: string
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return authFetch<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}

export async function listSessionFiles(path: string): Promise<FileListResponse> {
  const url = new URL(apiUrl("/api/files"));
  url.searchParams.set("path", path);
  return authFetch<FileListResponse>(url.toString());
}

/**
 * 生成文件下载地址。
 *
 * 浏览器 <a href> 直接下载无法携带 Authorization 请求头，
 * 因此下载接口额外支持 token 查询参数（服务端对该方式的取舍有注释说明）。
 */
export function getDownloadUrl(path: string): string {
  const url = new URL(apiUrl("/api/download"));
  url.searchParams.set("path", path);
  const token = getToken();
  if (token) {
    url.searchParams.set("token", token);
  }
  return url.toString();
}
