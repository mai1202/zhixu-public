import { defineStore } from 'pinia';
import { apiRequest, ApiError, setCsrfToken } from '../api/client';

export interface AuthUser {
  id: string;
  email: string;
  is_admin: boolean;
}

interface SessionResponse {
  user: AuthUser;
  csrf_token: string;
  expires_in: number;
}

interface VerifyResponse {
  user: AuthUser;
  csrf_token: string;
  expires_in: number;
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as AuthUser | null,
    initialized: false,
    loading: false,
    refreshAt: 0,
    sessionVersion: 0,
  }),
  actions: {
    clearSession() {
      // 防止退出或被撤销后，尚在途中的心跳响应重新恢复账户状态。
      this.sessionVersion += 1;
      this.user = null;
      this.refreshAt = 0;
      this.initialized = true;
      setCsrfToken(null);
    },
    async refreshSession() {
      const version = this.sessionVersion;
      const response = await apiRequest<SessionResponse>('/api/v1/auth/session');
      if (version !== this.sessionVersion) return;
      this.user = response.user;
      setCsrfToken(response.csrf_token);
      // 提前刷新，且最长五分钟核验一次撤销状态；后台暂停后唤醒仍可续期。
      this.refreshAt = Date.now() + Math.min(300_000, Math.max(1000, response.expires_in * 500));
    },
    async restore() {
      if (this.initialized) return;
      const version = this.sessionVersion;
      this.loading = true;
      try {
        await this.refreshSession();
        this.initialized = true; // 会话确认成功才算初始化完成。
      } catch (error) {
        if (version !== this.sessionVersion) return;
        this.user = null;
        setCsrfToken(null);
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
          // 4xx 表示确定不存在有效会话：完成初始化并由路由守卫引导到登录页。
          this.initialized = true;
        } else {
          // 网络异常或 5xx：保持未初始化，下一次路由切换会自动重试恢复会话。
          throw error;
        }
      } finally {
        this.loading = false;
      }
    },
    async verifyCode(challengeId: string, code: string) {
      const version = ++this.sessionVersion;
      const response = await apiRequest<VerifyResponse>('/api/v1/auth/verify-code', {
        method: 'POST',
        body: JSON.stringify({ challenge_id: challengeId, code }),
      });
      if (version !== this.sessionVersion) return;
      this.user = response.user;
      this.refreshAt = Date.now() + Math.min(300_000, Math.max(1000, response.expires_in * 500));
      this.initialized = true;
      setCsrfToken(response.csrf_token);
    },
    async logout() {
      const version = this.sessionVersion;
      await apiRequest<{ status: string }>('/api/v1/auth/logout', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (version === this.sessionVersion) this.clearSession();
    },
    async verifyPassword(email: string, password: string, policyVersion: string) {
      // 测试密码登录沿用会话版本，迟到响应不能覆盖已退出的状态。
      const version = ++this.sessionVersion;
      const response = await apiRequest<VerifyResponse>('/api/v1/auth/password-login', {
        method: 'POST', body: JSON.stringify({ email, password, policy_version: policyVersion }),
      });
      if (version !== this.sessionVersion) return;
      this.user = response.user;
      this.refreshAt = Date.now() + Math.min(300_000, Math.max(1000, response.expires_in * 500));
      this.initialized = true;
      setCsrfToken(response.csrf_token);
    },
  },
});
