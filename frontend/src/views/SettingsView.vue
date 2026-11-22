<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { statusLabel, taskKindLabel } from '../api/labels';
import {
  confirmPairedDevice,
  createDevicePairingCode,
  listPairedDevices,
  requestPairedDeviceSync,
  revokePairedDevice,
  savePairedDeviceSettings,
  type PairedDevice,
} from '../api/devices';
import { getClientDownload, type ClientDownloadInfo } from '../api/downloads';
import {
  createProviderProfile,
  deleteProviderProfile,
  getModelJob,
  listModelCatalog,
  listModelRuns,
  listProviderProfiles,
  getPlatformQuota,
  type PlatformQuota,
  testProviderProfile,
  updateProviderProfile,
  type ModelCatalogEntry,
  type ModelRun,
  type ProviderProfile,
} from '../api/models';
import AppIcon from '../components/AppIcon.vue';

const router = useRouter();
const route = useRoute();
const catalog = ref<ModelCatalogEntry[]>([]);
const profiles = ref<ProviderProfile[]>([]);
const platformQuota = ref<PlatformQuota | null>(null);
const rotatedKey = ref('');
const runs = ref<ModelRun[]>([]);
const label = ref('');
const provider = ref('deepseek');
const purpose = ref<ProviderProfile['purpose']>('extraction');
const executionTarget = ref<ProviderProfile['execution_target']>('server');
const deviceId = ref('');
const baseUrl = ref('');
const modelId = ref('deepseek-flash');
const apiKey = ref('');
const timeoutSeconds = ref(30);
const maxOutputTokens = ref(1500);
const fallbackPolicy = ref<ProviderProfile['fallback_policy']>('rules');
const loading = ref(false);
const busyId = ref<string | null>(null);
const errorMessage = ref('');
const successMessage = ref('');
const devices = ref<PairedDevice[]>([]);
const selectedChatIds = ref<Record<string, string[]>>({});
const syncIntervalMinutes = ref<Record<string, number>>({});
type CollectionDraft = { mode: 'all' | 'selected'; enabled: boolean; images: boolean; files: boolean; autoNotice: boolean };
const collectionDrafts = ref<Record<string, CollectionDraft>>({});

function collectionValues(device: PairedDevice): CollectionDraft {
  const settings = device.settings;
  return { mode: settings.chat_selection_mode ?? 'selected', enabled: settings.attachments_enabled,
    images: (settings.attachment_kinds ?? ['image', 'file']).includes('image'),
    files: (settings.attachment_kinds ?? ['image', 'file']).includes('file'), autoNotice: settings.attachment_auto_notice ?? true };
}

function changeCollectionMode(device: PairedDevice, event: Event): void {
  const draft = collectionDrafts.value[device.id];
  if (!draft) return;
  draft.mode = elementValue(event) as 'all' | 'selected';
  if (draft.mode === 'all') selectedChatIds.value[device.id] = device.settings.available_chats.map((chat) => chat.chat_id);
}

function collectionChecked(device: PairedDevice, field: 'enabled' | 'images' | 'files' | 'autoNotice', event: Event): void {
  if (collectionDrafts.value[device.id]) collectionDrafts.value[device.id][field] = Boolean((event.currentTarget as HTMLElement & { checked?: boolean }).checked);
}
const pairingCode = ref<{ code: string; expires_at_epoch: number } | null>(null);
const clientDownload = ref<ClientDownloadInfo | null>(null);
const clientDownloadError = ref('');
// 设置目录独立展示，每个编辑页面由路由固定所属分区。
const activeSettingsTab = computed(() => String(route.meta.settingsSection ?? 'devices'));
const sectionTitle = computed(() => ({devices: '设备与采集', models: '模型连接', runs: '运行诊断'}[activeSettingsTab.value] ?? '设置'));
const selectedCatalog = computed(() => catalog.value.find((item) => item.provider === provider.value));
const activeDeviceCount = computed(() => devices.value.filter((device) => device.status === 'active').length);
const enabledProfileCount = computed(() => profiles.value.filter((profile) => profile.enabled).length);
const failedRunCount = computed(() => runs.value.filter((run) => run.status === 'failed').length);
const latestRun = computed(() => runs.value[0] ?? null);
let disposed = false; // 离开页面后置 true：终止“测试连接”轮询，丢弃迟到回写。
let devicePollTimer: ReturnType<typeof setTimeout> | undefined;

async function pollDevices(): Promise<void> {
  // 串行轮询设备进度，保留尚未保存的选群；离开页面后停止更新。
  if (disposed) return;
  if (busyId.value === null) await refreshDevices(true);
  if (!disposed) devicePollTimer = setTimeout(() => { void pollDevices(); }, 5000);
}

onMounted(() => {
  void refresh();
  void refreshDevices();
  void refreshClientDownload();
  devicePollTimer = setTimeout(() => { void pollDevices(); }, 5000);
});

// 首页“去连接设备”使用独立地址直达采集设备分区。
onUnmounted(() => {
  disposed = true;
  clearTimeout(devicePollTimer);
});

function setError(error: unknown): void {
  errorMessage.value = error instanceof ApiError ? error.message : '操作失败，请稍后重试。';
  successMessage.value = '';
}

function elementValue(event: Event): string {
  // MDUI 组件把值放在宿主元素上，这里统一读取并保持脚本可读。
  return String((event.currentTarget as HTMLElement & { value?: string }).value ?? '');
}

function elementNumber(event: Event): number {
  const value = Number(elementValue(event));
  return Number.isFinite(value) ? value : 0;
}

// 分区层级由地址保持，刷新和分享链接仍会回到同一个设置页面。
function changeProviderField(event: Event): void {
  provider.value = elementValue(event);
  changeProvider();
}

function changePurposeField(event: Event): void {
  purpose.value = elementValue(event) as ProviderProfile['purpose'];
}

function changeExecutionField(event: Event): void {
  executionTarget.value = elementValue(event) as ProviderProfile['execution_target'];
  changeExecutionTarget();
}

function changeFallbackField(event: Event): void {
  fallbackPolicy.value = elementValue(event) as ProviderProfile['fallback_policy'];
}

function changeDeviceChat(deviceIdValue: string, chatId: string, event: Event): void {
  // 全选框由 MDUI 维护自身状态，这里只同步当前设备的允许会话数组。
  const checked = Boolean((event.currentTarget as HTMLElement & { checked?: boolean }).checked);
  const current = new Set(selectedChatIds.value[deviceIdValue] ?? []);
  if (checked) current.add(chatId);
  else current.delete(chatId);
  selectedChatIds.value[deviceIdValue] = [...current];
}

async function refresh(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    const [catalogResult, profileResult, runResult, quotaResult] = await Promise.all([
      listModelCatalog(), listProviderProfiles(), listModelRuns(), getPlatformQuota(),
    ]);
    catalog.value = catalogResult.items;
    profiles.value = profileResult.items;
    runs.value = runResult.items;
    platformQuota.value = quotaResult;
    if (!baseUrl.value) baseUrl.value = selectedCatalog.value?.default_base_url ?? '';
  } catch (error) {
    setError(error);
  } finally {
    loading.value = false;
  }
}

async function refreshClientDownload(): Promise<void> {
  clientDownloadError.value = '';
  try {
    clientDownload.value = await getClientDownload();
  } catch (error) {
    clientDownload.value = null;
    clientDownloadError.value = error instanceof ApiError ? error.message : '暂时无法读取客户端下载信息。';
  }
}

function downloadClient(): void {
  if (!clientDownload.value?.available || !clientDownload.value.url) return;
  window.location.assign(clientDownload.value.url);
}

function clientDownloadAbsoluteUrl(): string {
  const url = clientDownload.value?.url;
  if (!url) return '';
  return new URL(url, window.location.origin).href;
}

function formatFileSize(bytes: number | null): string {
  if (!bytes) return '';
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.ceil(bytes / 1024)} KB`;
}

async function refreshDevices(background = false): Promise<void> {
  try {
    const result = await listPairedDevices();
    if (disposed || (background && busyId.value !== null)) return;
    const previous = devices.value;
    devices.value = result.items;
    for (const device of result.items) {
      const old = previous.find((item) => item.id === device.id);
      const draft = collectionDrafts.value[device.id];
      const collectionEdited = old && draft && JSON.stringify(draft) !== JSON.stringify(collectionValues(old));
      if (!background || !collectionEdited) collectionDrafts.value[device.id] = collectionValues(device);
      // 没有本地草稿时接收 Agent 或其他页面保存的设置；有草稿时仅刷新进度。
      const chatDraft = selectedChatIds.value[device.id];
      const chatsEdited = old && chatDraft && JSON.stringify([...chatDraft].sort())
        !== JSON.stringify(old.settings.selected_chats.map((chat) => chat.chat_id).sort());
      if (!background || !chatsEdited) {
        selectedChatIds.value[device.id] = device.settings.selected_chats.map((chat) => chat.chat_id);
      }
      const intervalEdited = old && syncIntervalMinutes.value[device.id] !== undefined
        && syncIntervalMinutes.value[device.id] !== (old.settings.sync_interval_seconds ?? 300) / 60;
      if (!background || !intervalEdited) {
        syncIntervalMinutes.value[device.id] = (device.settings.sync_interval_seconds ?? 300) / 60;
      }
    }
  } catch (error) {
    if (!background && !disposed) setError(error);
  }
}

function initialSyncLabel(device: PairedDevice): string {
  // 仅已确认设备展示首轮状态；无消息的成功同步也能正常结束提示。
  const labels = {
    pending: '初次同步准备中，等待客户端连接并自动同步群目录。',
    running: '初次同步进行中，请稍候……',
    waiting_for_chat_selection: '群目录已同步，请勾选允许上传的会话并保存，随后自动进行初次消息同步。',
    partial: '初次同步部分完成，客户端将自动重试，也可在本地点击“立即同步”。',
    completed: '初次同步已完成。',
  };
  const initial = device.settings.initial_sync_status;
  if (initial && initial !== 'completed') return labels[initial];
  if (device.settings.sync_request_id) {
    if (device.settings.sync_request_id !== device.settings.sync_ack_id) {
      return device.settings.sync_result === 'running'
        ? '同步进行中，请稍候……' : '已请求立即同步，等待客户端响应；离线时将在恢复连接后执行。';
    }
    const results = {
      completed: '最近一次同步已完成。',
      partial: '最近一次同步部分完成，可点击立即同步重试。',
      waiting_for_chat_selection: '请先勾选允许上传的会话并保存。',
    };
    const status = device.settings.sync_result;
    if (status && status in results) return results[status as keyof typeof results];
  }
  return initial ? labels[initial] : '';
}

async function syncDeviceNow(device: PairedDevice): Promise<void> {
  // 发送请求与成功同步分开提示，避免离线设备被误报为已上传。
  busyId.value = `sync:${device.id}`;
  errorMessage.value = '';
  try {
    const result = await requestPairedDeviceSync(device.id);
    successMessage.value = result.note;
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
    await refreshDevices(true);
  }
}

async function refreshAll(): Promise<void> {
  await Promise.all([refresh(), refreshDevices(), refreshClientDownload()]);
}

async function generatePairingCode(): Promise<void> {
  busyId.value = 'pairing-code';
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const result = await createDevicePairingCode();
    pairingCode.value = { code: result.code, expires_at_epoch: result.expires_at_epoch };
    successMessage.value = '配对码仅可使用一次，并将在十分钟后过期。';
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

async function confirmDevice(device: PairedDevice): Promise<void> {
  busyId.value = `confirm:${device.id}`;
  errorMessage.value = '';
  try {
    await confirmPairedDevice(device.id);
    successMessage.value = `已确认设备“${device.name}”，客户端将自动进行初次同步。群目录出现后请选择允许同步的会话。`;
    await refreshDevices();
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

async function saveDeviceChats(device: PairedDevice): Promise<void> {
  busyId.value = `settings:${device.id}`;
  errorMessage.value = '';
  try {
    const selectedIds = new Set(selectedChatIds.value[device.id] ?? []);
    const selectedChats = device.settings.available_chats.filter((chat) => selectedIds.has(chat.chat_id));
    const minutes = syncIntervalMinutes.value[device.id] ?? 5;
    if (!Number.isInteger(minutes) || minutes < 1 || minutes > 60) {
      errorMessage.value = '同步间隔请输入 1～60 之间的整数分钟。';
      return;
    }
    const draft = collectionDrafts.value[device.id] ?? collectionValues(device);
    if (draft.enabled && !draft.images && !draft.files) {
      errorMessage.value = '开启附件采集时，请至少选择图片或文件。';
      return;
    }
    await savePairedDeviceSettings(device, selectedChats, minutes, {
      chat_selection_mode: draft.mode,
      excluded_chat_ids: draft.mode === 'all' ? device.settings.available_chats.filter((chat) => !selectedIds.has(chat.chat_id)).map((chat) => chat.chat_id) : [],
      attachments_enabled: draft.enabled, attachment_kinds: [...(draft.images ? ['image' as const] : []), ...(draft.files ? ['file' as const] : [])],
      attachment_auto_notice: draft.autoNotice,
    });
    successMessage.value = `采集设置已保存，每 ${minutes} 分钟同步一次；${draft.enabled ? '附件采集已开启' : '只采集文字'}。`;
    await refreshDevices();
  } catch (error) {
    setError(error);
    await refreshDevices();
  } finally {
    busyId.value = null;
  }
}

async function revokeDevice(device: PairedDevice): Promise<void> {
  if (!window.confirm(`撤销设备“${device.name}”？它的后续采集与上传会立即失效。`)) return;
  busyId.value = `revoke:${device.id}`;
  errorMessage.value = '';
  try {
    await revokePairedDevice(device.id);
    successMessage.value = '设备凭证已撤销。';
    await refreshDevices();
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

function changeProvider(): void {
  if (provider.value !== 'lm_studio') executionTarget.value = 'server';
  const selected = selectedCatalog.value;
  baseUrl.value = selected?.default_base_url ?? '';
  apiKey.value = '';
  if (provider.value === 'mimo') modelId.value = 'mimo-v2.6-pro';
  if (provider.value === 'openrouter') modelId.value = '';
  if (provider.value === 'custom_openai') modelId.value = '';
  if (provider.value === 'lm_studio') modelId.value = '';
  if (provider.value === 'deepseek') modelId.value = 'deepseek-flash';
}

function changeExecutionTarget(): void {
  if (executionTarget.value === 'device') {
    provider.value = 'lm_studio';
    baseUrl.value = selectedCatalog.value?.default_base_url ?? 'http://127.0.0.1:1234/v1';
    apiKey.value = '';
    if (!deviceId.value) deviceId.value = devices.value.find((device) => device.status === 'active')?.id ?? '';
  } else {
    deviceId.value = '';
  }
}

async function saveProfile(): Promise<void> {
  busyId.value = 'new';
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const saved = await createProviderProfile({
      label: label.value.trim(),
      provider: provider.value,
      purpose: purpose.value,
      ...(['lm_studio', 'custom_openai'].includes(provider.value) && baseUrl.value.trim() ? { base_url: baseUrl.value.trim() } : {}),
      model_id: modelId.value.trim(),
      execution_target: executionTarget.value,
      ...(executionTarget.value === 'device' && deviceId.value ? { device_id: deviceId.value } : {}),
      ...(apiKey.value ? { api_key: apiKey.value } : {}),
      timeout_seconds: timeoutSeconds.value,
      max_output_tokens: maxOutputTokens.value,
      fallback_policy: fallbackPolicy.value,
    });
    apiKey.value = '';
    label.value = '';
    profiles.value.unshift(saved);
    successMessage.value = '配置已保存且默认关闭。点击“测试连接”发送一条合成 JSON 请求，通过后再启用。';
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

/** 明确展示目标服务后，由用户亲自单独同意，不交给助手代为授权。 */
async function modelDataConsent(profile: ProviderProfile, enabled: boolean): Promise<void> {
  busyId.value = profile.id; errorMessage.value = '';
  try {
    const { apiRequest } = await import('../api/client');
    await apiRequest(`/api/v1/models/profiles/${profile.id}/data-consent`, { method: 'POST', body: JSON.stringify({ expected_version: profile.version, enabled }) });
    await refresh();
  } catch (failure) { setError(failure); }
  finally { busyId.value = null; }
}

async function testProfile(profile: ProviderProfile): Promise<void> {
  busyId.value = profile.id;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const result = await testProviderProfile(profile);
    if (result.status === 'queued' && result.job_id) {
      const deadline = Date.now() + 120_000;
      let job = await getModelJob(result.job_id);
      while (['queued', 'leased', 'running', 'retry_wait'].includes(job.status) && Date.now() < deadline && !disposed) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        if (disposed) break;
        try {
          job = await getModelJob(result.job_id);
        } catch {
          continue; // 单次查询抖动不改状态，下一轮重试。
        }
      }
      if (disposed) return; // 离开页面后任务仍在后台继续，无需提示。
      if (job.status !== 'succeeded') {
        throw new Error(['queued', 'leased', 'running', 'retry_wait'].includes(job.status)
          ? '等待本机设备连接超时；请确认客户端在线且 LM Studio 已加载该模型。'
          : `本机模型连接失败${job.error_code ? `（${job.error_code}）` : ''}。`);
      }
      successMessage.value = `本机连接成功：${profile.provider} / ${profile.model_id}；已验证 JSON Object 输出。`;
      await refresh();
      return;
    }
    successMessage.value = `连接成功：${result.provider} / ${result.model_id}；已验证 JSON Object 输出。`;
    await refresh();
  } catch (error) {
    await refresh();
    // 刷新会清空错误提示，必须在刷新状态后保留本次连接失败的具体原因。
    setError(error);
  } finally {
    busyId.value = null;
  }
}

async function toggleProfile(profile: ProviderProfile): Promise<void> {
  busyId.value = profile.id;
  errorMessage.value = '';
  try {
    const updated = await updateProviderProfile(profile, { enabled: !profile.enabled });
    profiles.value = profiles.value.map((item) => item.id === profile.id ? updated : item);
    successMessage.value = updated.enabled ? '模型配置已启用。' : '模型配置已停用。';
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

async function rotateKey(profile: ProviderProfile, value: string): Promise<void> {
  if (!value.trim()) return;
  busyId.value = profile.id;
  errorMessage.value = '';
  try {
    const updated = await updateProviderProfile(profile, { api_key: value });
    rotatedKey.value = '';
    profiles.value = profiles.value.map((item) => item.id === profile.id ? updated : item);
    successMessage.value = '新密钥已加密保存；配置已停用，需重新测试后启用。';
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

async function removeProfile(profile: ProviderProfile): Promise<void> {
  if (!window.confirm(`删除模型配置“${profile.label}”？`)) return;
  busyId.value = profile.id;
  errorMessage.value = '';
  try {
    await deleteProviderProfile(profile);
    profiles.value = profiles.value.filter((item) => item.id !== profile.id);
    successMessage.value = '模型配置已删除。';
    await refresh();
  } catch (error) {
    setError(error);
  } finally {
    busyId.value = null;
  }
}

function formatTime(epoch: number): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(epoch * 1000));
}

function capabilityLabel(value: string): string {
  const labels: Record<string, string> = {
    supported: '接口支持',
    api_supported_model_dependent: '接口支持，依模型核验',
    model_dependent: '依模型核验',
    route_dependent: '依路由核验',
    route_and_model_dependent: '依路由和模型核验',
    supported_for_model_subset: '限指定模型',
    unsupported_by_chat_api: '当前接口不支持',
    required_for_thinking_tool_roundtrip: '思考模式需保留历史',
    not_assumed: '不假定支持',
    protocol_claim_only: '仅协议声明',
    unverified: '未验证',
    unknown: '未知',
    deferred_for_product_data: '本产品暂缓',
  };
  return labels[value] ?? value;
}

function providerLabel(id: string): string {
  return catalog.value.find((item) => item.provider === id)?.label ?? id;
}

function deviceStatusLabel(status: PairedDevice['status']): string {
  return status === 'active' ? '已启用' : status === 'pending' ? '等待确认' : '已撤销';
}

function deviceTime(epoch: number | null): string {
  return epoch === null ? '尚无记录' : formatTime(epoch);
}
</script>

<template>
  <section class="page-section settings-page" aria-labelledby="settings-heading">
    <header class="settings-header">
      <div>
        <RouterLink class="app-link" to="/settings">← 返回设置目录</RouterLink>
        <h1 id="settings-heading">{{ sectionTitle }}</h1>
        <p>在此管理当前项目，保存后生效。</p>
      </div>
      <mdui-button variant="outlined" :disabled="loading || busyId !== null" @click="refreshAll">
        <AppIcon slot="icon" name="refresh"></AppIcon>
        刷新状态
      </mdui-button>
    </header>
    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <!-- 同一设置由独立页面负责读取和保存，复用已有授权逻辑。 -->
    <div v-if="activeSettingsTab === 'devices'" class="settings-tab-panel">
      <details class="settings-device-setup" :open="activeDeviceCount === 0">
        <summary>{{ activeDeviceCount ? '新增采集设备：下载与配对' : '首次连接：下载与配对' }}</summary>
      <mdui-card class="settings-section-card settings-client-download" variant="filled">
        <div class="settings-section-content settings-client-download-content">
          <div class="settings-client-download-copy">
            <span class="settings-kicker">Windows 采集客户端</span>
            <h2>下载并安装采集端</h2>
            <p>先在本页下载安装包，解压后双击 <code>采集客户端.cmd</code>。客户端只读取你在下方勾选允许上传的会话。</p>
            <div v-if="clientDownload?.available" class="settings-client-download-meta">
              <span>{{ clientDownload.filename }}</span>
              <span v-if="clientDownload.size_bytes">{{ formatFileSize(clientDownload.size_bytes) }}</span>
              <span v-if="clientDownload.sha256">SHA-256 {{ clientDownload.sha256.slice(0, 16) }}…</span>
            </div>
            <p v-else-if="clientDownloadError" class="helper-text">{{ clientDownloadError }}</p>
            <p v-else class="helper-text">安装包尚未发布到服务器；请联系管理员。</p>
          </div>
          <div class="settings-client-download-actions">
            <mdui-button variant="filled" :disabled="!clientDownload?.available" @click="downloadClient">
              <AppIcon slot="icon" name="arrow_downward"></AppIcon>
              下载 Windows 客户端
            </mdui-button>
            <a
              v-if="clientDownload?.available"
              class="settings-client-download-link app-link"
              :href="clientDownload.url ?? ''"
            >{{ clientDownloadAbsoluteUrl() }}</a>
          </div>
        </div>
      </mdui-card>

      <mdui-card class="settings-section-card" variant="outlined">
        <div class="settings-section-content">
          <div class="settings-section-heading">
            <div>
              <span class="settings-kicker">采集设备</span>
              <h2>Windows 采集端</h2>
              <p>新设备确认后默认不采集，勾选群聊后才开始。图片与文档附件可按需开启，间隔默认 5 分钟。</p>
            </div>
            <mdui-button variant="filled" :loading="busyId === 'pairing-code'" :disabled="busyId !== null" @click="generatePairingCode">
              生成配对码
            </mdui-button>
          </div>
          <div v-if="pairingCode" class="pairing-code-panel">
            <span>一次性配对码</span>
            <strong>{{ pairingCode.code }}</strong>
            <small>有效期至 {{ formatTime(pairingCode.expires_at_epoch) }}，仅可使用一次。</small>
          </div>

          <mdui-collapse class="settings-collapse">
            <mdui-collapse-item value="pairing-help">
              <mdui-list-item slot="header" headline="如何完成配对" description="打开采集客户端并粘贴配对码">
                <AppIcon slot="icon" name="help"></AppIcon>
              </mdui-list-item>
              <ol class="settings-help-list">
                <li>先从本页下载并解压 Windows 客户端，双击 <code>采集客户端.cmd</code>，确认服务地址并粘贴上方配对码。</li>
                <li>回到本页核对设备名称与指纹，然后点击“核对并确认”。</li>
                <li>客户端同步群目录后，勾选允许上传的群，按需开启附件与自动转化。</li>
              </ol>
            </mdui-collapse-item>
          </mdui-collapse>
        </div>
      </mdui-card>

      </details>
      <h2 v-if="devices.length">已配对设备 · {{ activeDeviceCount }} 台启用</h2>
      <div class="settings-stack">
        <mdui-card v-for="device in devices" :key="device.id" class="settings-device-card" variant="outlined">
          <div class="settings-section-content">
            <div class="settings-row-heading">
              <span class="settings-device-icon"><AppIcon name="computer"></AppIcon></span>
              <div>
                <h3>{{ device.name }}</h3>
                <p>指纹尾码 {{ device.fingerprint_hint }} · 创建于 {{ formatTime(device.created_at_epoch) }}</p>
              </div>
              <span class="profile-state" :data-enabled="device.status === 'active'">{{ deviceStatusLabel(device.status) }}</span>
            </div>
            <div class="settings-device-times">
              <span><small>最近心跳</small>{{ deviceTime(device.last_heartbeat_epoch) }}</span>
              <span><small>最近采集</small>{{ deviceTime(device.last_collect_epoch) }}</span>
              <span><small>最近上传</small>{{ deviceTime(device.last_upload_epoch) }}</span>
            </div>
            <!-- 首轮进度随设备轮询刷新，辅助技术也能获知状态变化。 -->
            <p v-if="device.status === 'active' && initialSyncLabel(device)" role="status" aria-live="polite">{{ initialSyncLabel(device) }}</p>
            <section v-if="device.status === 'active' && collectionDrafts[device.id]" class="settings-collection-fields">
              <h4>1 · 群范围</h4>
              <mdui-select label="接管范围" :value="collectionDrafts[device.id]!.mode" @change="changeCollectionMode(device, $event)">
                <mdui-menu-item value="all">全部已发现群（新群自动加入）</mdui-menu-item>
                <mdui-menu-item value="selected">仅逐项选择的群</mdui-menu-item>
              </mdui-select>
              <p class="helper-text">下方勾选列表就是实际采集范围；全部群模式下取消勾选即排除该群。</p>
              <h4>2 · 附件采集与转化</h4>
              <mdui-checkbox :checked="collectionDrafts[device.id]!.enabled" @change="collectionChecked(device, 'enabled', $event)">采集群里的图片和文件附件</mdui-checkbox>
              <div v-if="collectionDrafts[device.id]!.enabled" class="settings-attachment-options">
                <mdui-checkbox :checked="collectionDrafts[device.id]!.images" @change="collectionChecked(device, 'images', $event)">图片（PNG / JPEG，经过 OCR 识别）</mdui-checkbox>
                <mdui-checkbox :checked="collectionDrafts[device.id]!.files" @change="collectionChecked(device, 'files', $event)">文档（文本 / PDF / Word / Excel）</mdui-checkbox>
                <mdui-checkbox :checked="collectionDrafts[device.id]!.autoNotice" @change="collectionChecked(device, 'autoNotice', $event)">解析附件后自动转化为通知</mdui-checkbox>
                <p class="helper-text">关闭自动转化后只保存附件。通知是否自动维护事务和课程，在“通知处理”中单独设置。</p>
              </div>
              <h4>3 · 同步频率</h4>
            </section>
            <!-- 间隔与群范围一起保存，轮询不会覆盖用户正在编辑的值。 -->
            <mdui-text-field v-if="device.status === 'active'" label="自动同步间隔（分钟）" type="number" min="1" max="60" step="1"
              :value="String(syncIntervalMinutes[device.id] ?? 5)" helper="1～60 分钟，默认 5 分钟，保存后生效。立即同步使用已保存的群范围。"
              @input="syncIntervalMinutes[device.id] = elementNumber($event)"></mdui-text-field>
            <div v-if="device.status === 'pending'" class="settings-actions">
              <mdui-button variant="filled" :loading="busyId === `confirm:${device.id}`" :disabled="busyId !== null" @click="confirmDevice(device)">核对并确认</mdui-button>
            </div>
            <mdui-collapse v-if="device.status === 'active'" class="device-chat-collapse">
              <mdui-collapse-item value="chats">
                <mdui-list-item
                  slot="header"
                  headline="允许上传的会话"
                  :description="`已选 ${(selectedChatIds[device.id] ?? []).length} / ${device.settings.available_chats.length}`"
                >
                  <AppIcon slot="icon" name="chat"></AppIcon>
                </mdui-list-item>
                <div class="device-chat-list">
                  <mdui-checkbox
                    v-for="chat in device.settings.available_chats"
                    :key="chat.chat_id"
                    :checked="(selectedChatIds[device.id] ?? []).includes(chat.chat_id)"
                    @change="changeDeviceChat(device.id, chat.chat_id, $event)"
                  >{{ chat.chat_name }}</mdui-checkbox>
                  <p v-if="device.settings.available_chats.length === 0" class="empty-note">等待客户端同步群目录。未勾选群时不会上传正文。</p>
                </div>
              </mdui-collapse-item>
            </mdui-collapse>
            <div v-if="device.status !== 'revoked'" class="settings-actions">
              <mdui-button v-if="device.status === 'active'" variant="outlined" :loading="busyId === `settings:${device.id}`" :disabled="busyId !== null" @click="saveDeviceChats(device)">保存采集设置</mdui-button>
              <mdui-button v-if="device.status === 'active'" variant="filled" :loading="busyId === `sync:${device.id}`" :disabled="busyId !== null" @click="syncDeviceNow(device)">立即同步</mdui-button>
              <mdui-button variant="text" :disabled="busyId !== null" @click="revokeDevice(device)">解除配对</mdui-button>
            </div>
          </div>
        </mdui-card>
        <div v-if="devices.length === 0" class="settings-empty-state">
          <AppIcon name="computer"></AppIcon>
          <strong>尚未配对采集设备</strong>
          <p>生成配对码后，在 Windows 采集客户端中完成连接。</p>
        </div>
      </div>
    </div>

    <div v-if="activeSettingsTab === 'models'" class="settings-tab-panel">
      <mdui-card v-if="platformQuota" class="settings-section-card" variant="outlined">
        <div class="settings-section-content">
          <h2>今日平台模型额度</h2>
          <p v-if="platformQuota.exempt">此账户豁免平台每日 3 元预算限制，仍记录模型用量与费用。</p>
          <p v-else>每日上限 ¥{{ platformQuota.limit_cny }}；已计费 ¥{{ platformQuota.spent_cny.toFixed(4) }}，调用中预留 ¥{{ platformQuota.reserved_cny.toFixed(4) }}，可用 ¥{{ platformQuota.remaining_cny?.toFixed(4) }}。</p>
          <p class="helper-text">按缓存输入、未缓存输入与输出分别计价，北京时间每天零点重置。{{ platformQuota.self_configured_excluded ? '自有密钥不占平台额度。' : '此测试账号的云端模型均受日预算限制。' }}回执缺失按预留上界估算。</p>
          <RouterLink class="app-link" to="/agent/workspace">在 Agent 工作台查看计费明细 →</RouterLink>
        </div>
      </mdui-card>
      <mdui-collapse class="settings-collapse settings-add-model">
        <mdui-collapse-item value="add-model">
          <mdui-list-item slot="header" headline="添加模型配置" description="只在连接新模型时展开">
            <AppIcon slot="icon" name="add_circle"></AppIcon>
          </mdui-list-item>
          <form class="settings-model-form" @submit.prevent="saveProfile">
            <mdui-text-field label="显示名称" :value="label" maxlength="100" required placeholder="例如：个人 DeepSeek" @input="label = elementValue($event)"></mdui-text-field>
            <mdui-select variant="outlined" label="提供方" :value="provider" @change="changeProviderField">
              <mdui-menu-item v-for="item in catalog" :key="item.provider" :value="item.provider" :disabled="!item.configurable">
                {{ item.label }}{{ item.configurable ? '' : ' · 暂缓' }}
              </mdui-menu-item>
            </mdui-select>
            <mdui-select variant="outlined" label="用途" :value="purpose" @change="changePurposeField">
              <mdui-menu-item value="extraction">通知结构化</mdui-menu-item>
              <mdui-menu-item value="planning">对话规划</mdui-menu-item>
              <mdui-menu-item value="mail_suggestion">邮件建议</mdui-menu-item>
            </mdui-select>
            <mdui-select variant="outlined" label="执行位置" :value="executionTarget" @change="changeExecutionField">
              <mdui-menu-item value="server">服务器</mdui-menu-item>
              <mdui-menu-item value="device" :disabled="provider !== 'lm_studio'">已配对 Windows 设备（仅 LM Studio）</mdui-menu-item>
            </mdui-select>
            <mdui-select v-if="executionTarget === 'device'" variant="outlined" label="绑定设备" :value="deviceId" required @change="deviceId = elementValue($event)">
              <mdui-menu-item value="" disabled>选择已确认设备</mdui-menu-item>
              <mdui-menu-item v-for="device in devices.filter((item) => item.status === 'active')" :key="device.id" :value="device.id">{{ device.name }}</mdui-menu-item>
            </mdui-select>
            <mdui-text-field label="模型 ID" :value="modelId" maxlength="200" required placeholder="按服务商文档填写" @input="modelId = elementValue($event)"></mdui-text-field>
            <mdui-text-field
              v-if="provider === 'lm_studio' || provider === 'custom_openai'"
              class="settings-wide-field"
              label="base_url"
              :value="baseUrl"
              maxlength="500"
              :required="provider === 'custom_openai'"
              @input="baseUrl = elementValue($event)"
            ></mdui-text-field>
            <mdui-text-field
              v-if="selectedCatalog?.requires_key"
              class="settings-wide-field"
              label="API 密钥"
              type="password"
              :value="apiKey"
              autocomplete="new-password"
              minlength="8"
              required
              toggle-password
              @input="apiKey = elementValue($event)"
            ></mdui-text-field>
            <mdui-text-field label="超时（秒）" type="number" :value="String(timeoutSeconds)" min="3" max="120" @input="timeoutSeconds = elementNumber($event)"></mdui-text-field>
            <mdui-text-field label="最大输出 token" type="number" :value="String(maxOutputTokens)" min="64" max="8192" @input="maxOutputTokens = elementNumber($event)"></mdui-text-field>
            <mdui-select variant="outlined" label="失败回退" :value="fallbackPolicy" @change="changeFallbackField">
              <mdui-menu-item value="rules">仅回退到本地规则</mdui-menu-item>
              <mdui-menu-item value="none">不回退，保留待核验材料</mdui-menu-item>
            </mdui-select>
            <p class="helper-text settings-wide-field">连接测试只发送合成内容。云模型会收到你选择进行模型分析的通知正文。</p>
            <div class="settings-form-actions settings-wide-field">
              <mdui-button type="submit" variant="filled" :loading="busyId === 'new'" :disabled="busyId !== null || !modelId.trim() || !label.trim() || (executionTarget === 'device' && !deviceId)">
                保存模型配置
              </mdui-button>
            </div>
          </form>
        </mdui-collapse-item>
      </mdui-collapse>

      <div class="settings-stack">
        <mdui-card v-for="profile in profiles" :key="profile.id" class="settings-model-card" variant="outlined">
          <div class="settings-section-content">
            <div class="settings-row-heading">
              <span class="settings-model-icon"><AppIcon name="hub"></AppIcon></span>
              <div>
                <h3>{{ profile.label }}</h3>
                <p>{{ providerLabel(profile.provider) }} · {{ profile.model_id }}</p>
              </div>
              <span class="profile-state" :data-enabled="profile.enabled">{{ profile.is_default ? '服务器默认' : profile.enabled ? '已启用' : profile.last_test_status === 'success' ? '已通过测试' : '未启用' }}</span>
            </div>
            <div class="settings-model-meta">
              <span>{{ profile.purpose === 'extraction' ? '通知提取' : profile.purpose === 'planning' ? '对话规划' : '邮件建议' }}</span>
              <span>{{ profile.execution_target === 'server' ? '服务器执行' : '设备执行' }}</span>
              <span>{{ profile.fallback_policy === 'rules' ? '规则回退' : '保留待核验' }}</span>
              <span>{{ profile.is_default ? '服务器统一密钥' : profile.has_api_key ? '密钥已保存' : '本地服务' }}</span>
            </div>
            <p v-if="profile.is_default" class="helper-text">由服务器统一配置，所有登录账户可直接使用；密钥不会在页面回显。</p>
            <section v-if="profile.needs_data_consent" class="helper-text">
              <p>此服务位于境外或处理位置尚未确认：{{ profile.base_url }}。使用时会发送当前任务所需的通知、对话、图片及相关资料，请先查看该服务的隐私说明。</p>
              <mdui-button variant="outlined" :disabled="busyId !== null" @click="modelDataConsent(profile, !profile.data_consent)">
                {{ profile.data_consent ? '撤回对该配置的资料处理同意' : '单独同意向该服务发送必要资料' }}
              </mdui-button>
            </section>
            <div class="settings-actions">
              <mdui-button variant="outlined" :loading="busyId === profile.id" :disabled="busyId !== null" @click="testProfile(profile)">测试连接</mdui-button>
              <template v-if="!profile.is_default">
                <mdui-button variant="filled" :disabled="busyId !== null || (!profile.enabled && profile.last_test_status !== 'success')" @click="toggleProfile(profile)">
                  {{ profile.enabled ? '停用' : '启用' }}
                </mdui-button>
                <mdui-button variant="text" :disabled="busyId !== null" @click="removeProfile(profile)">删除</mdui-button>
              </template>
            </div>
            <mdui-collapse class="settings-collapse settings-advanced">
              <mdui-collapse-item value="details">
                <mdui-list-item slot="header" headline="高级与技术详情" description="能力矩阵、密钥轮换">
                  <AppIcon slot="icon" name="settings"></AppIcon>
                </mdui-list-item>
                <ul class="capability-list">
                  <li v-for="(value, key) in profile.capabilities" :key="key"><strong>{{ key }}</strong>：{{ capabilityLabel(value) }}</li>
                </ul>
                <form v-if="profile.has_api_key && !profile.is_default" class="rotate-key-form" @submit.prevent="rotateKey(profile, rotatedKey)">
                  <mdui-text-field label="替换 API 密钥" type="password" :value="rotatedKey" autocomplete="new-password" minlength="8" @input="rotatedKey = elementValue($event)"></mdui-text-field>
                  <mdui-button type="submit" variant="text" :disabled="busyId !== null || !rotatedKey.trim()">保存新密钥</mdui-button>
                </form>
              </mdui-collapse-item>
            </mdui-collapse>
          </div>
        </mdui-card>
        <div v-if="!loading && profiles.length === 0" class="settings-empty-state">
          <AppIcon name="hub"></AppIcon>
          <strong>尚无模型配置</strong>
          <p>通知规则路径仍可独立使用；连接模型后可以启用摘要与规划。</p>
        </div>
      </div>

      <mdui-collapse class="settings-collapse settings-advanced">
        <mdui-collapse-item value="catalog">
          <mdui-list-item slot="header" headline="提供方能力矩阵" description="服务端协议声明与产品限制">
            <AppIcon slot="icon" name="schema"></AppIcon>
          </mdui-list-item>
          <p class="helper-text settings-matrix-help">标记表达服务端协议声明；模型、版本和路由能力必须通过具体配置测试。</p>
          <div v-for="entry in catalog" :key="entry.provider" class="catalog-row">
            <strong>{{ entry.label }}</strong>
            <span>{{ entry.api_family }} · {{ entry.configurable ? '可配置' : entry.deferred_reason }}</span>
            <small v-for="(value, key) in entry.capabilities" :key="key">{{ key }}：{{ capabilityLabel(value) }}</small>
          </div>
        </mdui-collapse-item>
      </mdui-collapse>
    </div>

    <div v-if="activeSettingsTab === 'runs'" class="settings-tab-panel">
      <mdui-card class="settings-section-card" variant="outlined">
        <div class="settings-section-content">
          <div class="settings-section-heading">
            <div>
              <span class="settings-kicker">运行记录</span>
              <h2>最近模型调用</h2>
              <p>记录 token、延迟、费用和失败原因，便于判断连接是否稳定。</p>
            </div>
            <mdui-button variant="text" :disabled="loading || busyId !== null" @click="refresh">刷新</mdui-button>
          </div>
          <div class="settings-run-list">
            <div v-for="run in runs" :key="run.id" class="settings-run-row">
              <span class="settings-run-status" :data-status="run.status">{{ statusLabel(run.status) }}</span>
              <div>
                <strong>{{ taskKindLabel(run.task_kind) }} · {{ providerLabel(run.provider) }} / {{ run.model_id }}</strong>
                <small>{{ formatTime(run.created_at_epoch) }}<template v-if="run.failure_code"> · {{ run.failure_code }}</template></small>
              </div>
              <span class="settings-run-metrics">
                {{ run.prompt_tokens ?? '—' }} / {{ run.completion_tokens ?? '—' }} tokens · {{ run.latency_ms }} ms
              </span>
            </div>
            <p v-if="!runs.length" class="empty-note">尚无模型调用记录。</p>
          </div>
        </div>
      </mdui-card>
    </div>
  </section>
</template>

<style scoped>
/* 一级分区按工作流排列，二级目录展示具体设置和作用，手机保持单列。 */
.settings-search { display: block; margin: 12px 0; }
.settings-navigation-groups { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.settings-directory-heading p { color: var(--app-on-surface-variant); }
.settings-directory-entries { display: grid; }
.settings-directory-entries a { position: relative; display: grid; gap: 7px; padding: 16px 26px 16px 0; color: inherit; text-decoration: none; border-top: 1px solid var(--app-outline); }
.settings-directory-entries a:hover strong { color: var(--app-primary); }
.settings-directory-entries span { font-size: 13px; line-height: 1.6; color: var(--app-on-surface-variant); }
.settings-directory-entries .app-icon { position: absolute; right: 0; top: 20px; width: 18px; }
.settings-processing-entry { padding: 20px; border: 1px solid var(--app-outline); border-radius: 16px; scroll-margin-top: 150px; }
.settings-processing-flow { display: grid; gap: 20px; padding: 0; list-style: none; }
.settings-processing-flow p { margin: 8px 0; line-height: 1.75; color: var(--app-on-surface-variant); }
.settings-processing-links { display: flex; flex-wrap: wrap; gap: 16px; }
.settings-collection-fields, .settings-attachment-options { display: grid; gap: 10px; }
.settings-collection-fields h4 { margin: 16px 0 0; }
.settings-flow-help { margin-top: 18px; }
.settings-flow-help summary, .settings-device-setup > summary { cursor: pointer; padding: 12px 0; font-weight: 500; }
.settings-device-setup[open] { display: grid; gap: 12px; }
@media (max-width: 900px) { .settings-navigation-groups { grid-template-columns: 1fr; } }
</style>
