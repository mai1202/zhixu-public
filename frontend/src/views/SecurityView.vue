<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { apiRequest, ApiError } from '../api/client';
import { useAuthStore } from '../stores/auth';

interface LoginDevice {
  id: string; name: string; is_current: boolean; persistent: boolean;
  created_at_epoch: number; last_seen_at_epoch: number; expires_at_epoch: number | null;
}
const router = useRouter();
const auth = useAuthStore();
const devices = ref<LoginDevice[]>([]);
const loading = ref(false);
const pending = ref<LoginDevice | null>(null);
const revoking = ref(false);
const error = ref('');
const notice = ref('');

async function load(): Promise<void> {
  loading.value = true;
  error.value = '';
  try { devices.value = (await apiRequest<{items: LoginDevice[]}>('/api/v1/auth/sessions')).items; }
  catch (failure) { error.value = failure instanceof ApiError ? failure.message : '登录设备加载失败，请重试。'; }
  finally { loading.value = false; }
}
function date(value: number): string { return new Date(value * 1000).toLocaleString('zh-CN', {hour12: false}); }
async function revoke(): Promise<void> {
  // 只撤销确认框所对应的会话，不影响该设备采集器配对或其他设备登录。
  const target = pending.value;
  if (!target || revoking.value) return;
  revoking.value = true;
  error.value = '';
  try {
    const result = await apiRequest<{is_current: boolean}>('/api/v1/auth/sessions/' + encodeURIComponent(target.id), {method: 'DELETE'});
    pending.value = null;
    devices.value = devices.value.filter(item => item.id !== target.id);
    if (result.is_current) {
      auth.clearSession();
      await router.replace('/login');
    } else { notice.value = '已撤销此设备的登录，需要重新验证邮箱才能登录。'; }
  } catch (failure) { error.value = failure instanceof ApiError ? failure.message : '撤销失败，请重试。'; }
  finally { revoking.value = false; }
}
onMounted(load);
</script>

<template>
  <section class="page-section security-page" aria-labelledby="security-heading">
    <RouterLink class="app-link" to="/settings">← 返回设置目录</RouterLink>
    <header><p class="eyebrow">设置 / 安全</p><h1 id="security-heading">安全</h1>
      <p>允许多设备同时登录，默认长期保持登录，使用时自动续期。可以随时撤销某个设备的登录。</p>
      <p class="helper-text">浏览器清除站点数据后需要重新登录。下方管理的是网页登录，不影响采集器配对。</p>
    </header>
    <div class="security-heading"><h2>登录设备</h2><mdui-button variant="text" :disabled="loading || revoking" @click="load">{{ loading ? '刷新中…' : '刷新列表' }}</mdui-button></div>
    <p v-if="error && !pending" class="error-text" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading && !devices.length" role="status">正在加载登录设备…</p>
    <ul class="security-devices">
      <li v-for="device in devices" :key="device.id" class="security-device">
        <div><h3>{{ device.name }} <span v-if="device.is_current" class="security-current">当前设备</span></h3>
          <p>登录时间：{{ date(device.created_at_epoch) }}</p><p>最近活动：{{ date(device.last_seen_at_epoch) }}</p>
          <p>{{ device.persistent ? '登录期限：长期有效，自动续期' : '登录到期：' + date(device.expires_at_epoch!) }}</p>
        </div>
        <mdui-button variant="outlined" :disabled="loading || revoking" :aria-label="'撤销 ' + device.name + (device.is_current ? '（当前设备）' : '') + ' 的登录'" @click="pending = device; error = ''; notice = ''">{{ device.is_current ? '退出当前设备' : '撤销登录' }}</mdui-button>
      </li>
    </ul>
    <!-- 敏感操作明确展示目标；出错后保留确认框，便于重试。 -->
    <mdui-dialog :open="!!pending" headline="撤销设备登录" :description="pending ? '确定撤销 ' + pending.name + (pending.is_current ? '（当前设备）' : '') + ' 的登录？该设备需要重新验证邮箱。' : ''" @close="!revoking && (pending = null)">
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <mdui-button slot="action" variant="text" :disabled="revoking" @click="pending = null">取消</mdui-button>
      <mdui-button slot="action" variant="filled" :disabled="revoking" @click="revoke">{{ revoking ? '正在撤销…' : '确认撤销' }}</mdui-button>
    </mdui-dialog>
  </section>
</template>

<style scoped>
/* 底部留出助手浮钮空间，窄屏操作放在左侧，避免与右下角助手重叠。 */
.security-page{max-width:1000px;padding-bottom:100px}.security-page header{margin:24px 0}.security-page header p{line-height:1.7}.security-heading{display:flex;align-items:center;justify-content:space-between;gap:16px}.security-devices{list-style:none;padding:0}.security-device{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:22px 0;border-bottom:1px solid var(--app-outline-variant,#dce1eb)}.security-device h3{margin:0 0 12px;font-size:18px;overflow-wrap:anywhere}.security-device p{margin:6px 0;font-size:14px}.security-current{display:inline-block;margin-left:8px;padding:3px 8px;border-radius:8px;background:var(--app-surface-container,#eef2fa);font-size:12px}.security-device mdui-button{flex-shrink:0}@media(max-width:600px){.security-device{align-items:flex-start;flex-direction:column;gap:12px}.security-device mdui-button{align-self:flex-start}}
</style>
