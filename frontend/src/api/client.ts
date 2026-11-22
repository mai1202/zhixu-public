import type { HealthResponse } from '@notification-management/contracts';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly requestId?: string,
    readonly details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  // CSRF 值只保留在当前页面内存，不写入 localStorage 或 Pinia 持久化。
  csrfToken = token;
}

export async function apiRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  // 用发出请求时的会话快照识别迟到的 401，避免旧设备请求清掉刚完成的新登录。
  const requestCsrfToken = csrfToken;
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) {
    headers.set('X-CSRF-Token', csrfToken);
  }

  const response = await fetch(path, { ...init, headers });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    // 已撤销的设备在下一次请求时统一清理登录态，避免仍展示为已登录。
    if (response.status === 401 && body?.error?.code === 'authentication_required' && csrfToken === requestCsrfToken) {
      window.dispatchEvent(new Event('auth-session-expired'));
    }
    throw new ApiError(
      body?.error?.message ?? '请求失败，请稍后重试。',
      response.status,
      typeof body?.error?.code === 'string' ? body.error.code : undefined,
      body?.request_id ?? response.headers.get('X-Request-ID') ?? undefined,
      body?.error?.details && typeof body.error.details === 'object'
        ? body.error.details as Record<string, unknown>
        : undefined,
    );
  }
  return body as T;
}

export async function getHealth(): Promise<HealthResponse> {
  return apiRequest<HealthResponse>('/api/v1/health');
}
