interface SessionState {
  user: unknown;
  refreshAt: number;
  clearSession(): void;
  refreshSession(): Promise<void>;
}

export function startSessionMaintenance(auth: SessionState, onExpired: () => void): () => void {
  let pending = false;
  let stopped = false;
  async function refresh(force = false): Promise<void> {
    if (stopped || pending || !auth.user || (!force && Date.now() < auth.refreshAt)) return;
    pending = true;
    try { await auth.refreshSession(); }
    catch {
      // 网络暂不可用时保留登录态并退避，避免断网后每秒重试；401 由公共请求层处理。
      auth.refreshAt = Date.now() + 30_000;
    }
    finally { pending = false; }
  }
  function expired(): void {
    if (!auth.user) return;
    auth.clearSession();
    onExpired();
  }
  function focus(): void { void refresh(true); }
  function visible(): void { if (document.visibilityState === 'visible') void refresh(true); }
  // 不向本地存储写令牌，也不创建第二套凭据；HttpOnly Cookie 由服务器自动续期。
  const timer = window.setInterval(() => { void refresh(); }, 1000);
  window.addEventListener('focus', focus);
  window.addEventListener('auth-session-expired', expired);
  document.addEventListener('visibilitychange', visible);
  return () => {
    stopped = true;
    window.clearInterval(timer);
    window.removeEventListener('focus', focus);
    window.removeEventListener('auth-session-expired', expired);
    document.removeEventListener('visibilitychange', visible);
  };
}
