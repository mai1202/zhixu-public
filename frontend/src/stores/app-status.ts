import { defineStore } from 'pinia';
import { getHealth } from '../api/client';
import type { HealthResponse } from '@notification-management/contracts';

export const useAppStatusStore = defineStore('app-status', {
  state: () => ({
    health: null as HealthResponse | null,
    loading: false,
    error: '',
  }),
  actions: {
    async refreshHealth() {
      this.loading = true;
      this.error = '';
      try {
        this.health = await getHealth();
      } catch {
        this.error = '无法连接服务端，请检查服务是否启动。';
      } finally {
        this.loading = false;
      }
    },
  },
});
