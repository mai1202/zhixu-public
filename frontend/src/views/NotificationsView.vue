<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, toRaw, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import type { OcrItem, OcrResult } from '@notification-management/contracts';
import { recognizeImage, type OcrModel, type OcrProgress, type OcrProgressHandler } from '../../../packages/ocr-runtime/src';
import { apiRequest, ApiError } from '../api/client';
import { createTextSource, getInboxItem } from '../api/business';
import { createOcrSource, getOcrArtifact, submitBrowserOcr, submitServerOcr, type OcrArtifact } from '../api/ocr';
import {
  getNotification,
  getNotificationCounts,
  listNotifications,
  updateNotification,
  type Mailbox,
  type NotificationCounts,
  type NotificationMessage as Message,
} from '../api/notifications';
import NoticeEvidence from '../components/NoticeEvidence.vue';
import CollectedAttachments from '../components/CollectedAttachments.vue';
import AgentRuntimeAlert from '../components/AgentRuntimeAlert.vue';
import { sourceSubmission } from '../api/submission';
import { useAuthStore } from '../stores/auth';
import { readMduiValue } from '../utils/mdui';
import { formatRelativeDateTime } from '../utils/datetime';
import AppIcon from '../components/AppIcon.vue';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const mailbox = ref<Mailbox>(['ignored', 'archived'].includes(String(route.query.panel)) ? route.query.panel as Mailbox : 'inbox');
// 邮箱分组是列表筛选，和搜索放在同一个工具区。
const mailboxOptions: { value: Mailbox; label: string }[] = [
  { value: 'inbox', label: '收件箱' },
  { value: 'archived', label: '归档' },
  { value: 'ignored', label: '忽略' },
];
const items = ref<Message[]>([]);
const selected = ref<Message | null>(null);
const query = ref(String(route.query.q ?? ''));
const searchOpen = ref(Boolean(query.value));
const searchField = ref<HTMLElement | null>(null);
const counts = ref<NotificationCounts>({ inbox: 0, archived: 0, ignored: 0, unread: 0 });
const actionId = ref('');
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const status = ref('');
const hasMore = ref(false);
// 系统分享只预填资料，绝不自动提交；未登录时由既有登录回跳保留完整入口。
const sharedDraft = [route.query.shared_title, route.query.shared_text, route.query.shared_url]
  .filter((value): value is string => typeof value === 'string').join('\n').slice(0, 100000);
const text = ref(sharedDraft);
const composing = ref(route.query.compose === '1' || Boolean(sharedDraft));
// 通知页的截图 OCR：识别结果先在本机展示并允许逐行修正，再保存为带原图的来源。
const ocrOpen = ref(false);
const ocrInput = ref<HTMLInputElement | null>(null);
const ocrFile = ref<File | null>(null);
const ocrImageUrl = ref('');
const ocrResult = ref<OcrResult | null>(null);
const ocrItems = ref<OcrItem[]>([]);
const ocrArtifact = ref<OcrArtifact | null>(null);
const ocrTitle = ref('');
const ocrLocation = ref<'browser' | 'server'>('browser');
const ocrModel = ref<OcrModel>('tiny');
const ocrBusy = ref(false);
const ocrProgress = ref<OcrProgress | null>(null);
const ocrError = ref('');
let selectToken = 0;
let listToken = 0;
let countsToken = 0;
let timer: ReturnType<typeof setInterval> | undefined;
let disposed = false;

function fail(failure: unknown) {
  if (disposed) return;
  error.value = failure instanceof ApiError ? failure.message : '操作失败，请稍后重试。';
}

async function refresh(append = false) {
  if (disposed) return;
  const token = ++listToken;
  loading.value = true;
  error.value = '';
  const currentMailbox = mailbox.value;
  try {
    const result = await listNotifications(currentMailbox, query.value, append ? items.value.length : 0);
    if (disposed || mailbox.value !== currentMailbox || token !== listToken) return;
    items.value = append ? [...items.value, ...result.items] : result.items;
    hasMore.value = result.has_more;
  } catch (failure) {
    if (!disposed && token === listToken) fail(failure);
  } finally {
    if (!disposed && token === listToken) loading.value = false;
  }
}

async function refreshCounts(): Promise<void> {
  if (disposed) return;
  const token = ++countsToken;
  try {
    const result = await getNotificationCounts();
    if (!disposed && token === countsToken) counts.value = result;
  } catch {
    // 计数是增强信息；失败不覆盖列表上的错误提示。
  }
}

function listLocation(next = mailbox.value) {
  return {
    path: '/inbox',
    query: {
      ...(next !== 'inbox' ? { panel: next } : {}),
      ...(query.value ? { q: query.value } : {}),
    },
  };
}

function changeMailbox(next: Mailbox) {
  if (next === mailbox.value) return;
  status.value = '';
  void router.push(listLocation(next));
}

function searchMessages() {
  void router.push(listLocation()).then(() => {
    void refresh();
    void refreshCounts();
  });
}

async function openNotificationSearch() {
  // 搜索按需展开并获得焦点，收件箱仍优先显示列表。
  searchOpen.value = true;
  await nextTick();
  searchField.value?.focus();
}

function openMessage(id: string) {
  return router.push({ ...listLocation(), path: '/inbox/' + encodeURIComponent(id) });
}

function closeMessage() {
  if (disposed) return Promise.resolve();
  selectToken += 1;
  selected.value = null;
  return router.push(listLocation());
}

// 读取与写已读都受选择序号保护，后退或改选后不能回写旧详情。
async function loadMessage(id: string) {
  if (disposed) return;
  const token = ++selectToken;
  error.value = '';
  try {
    const detail = await getNotification(id);
    if (disposed || token !== selectToken) return;
    const state = detail.read ? detail : await updateNotification(detail.id, { read: true }, detail.version);
    if (disposed || token !== selectToken) return;
    // 废弃打开前发出的列表读取，避免旧响应把已经读过的通知重新画成未读。
    listToken++;
    loading.value = false;
    selected.value = { ...detail, ...state };
    const index = items.value.findIndex((item) => item.id === detail.id);
    if (index >= 0) items.value[index] = state;
    void refreshCounts();
  } catch (failure) {
    if (token === selectToken) fail(failure);
  }
}

// URL 决定选中对象；刷新、前进、后退都恢复同一个筛选与阅读状态。
async function syncRoute() {
  const token = ++selectToken;
  selected.value = null;
  mailbox.value = ['archived', 'ignored'].includes(String(route.query.panel)) ? route.query.panel as Mailbox : 'inbox';
  query.value = String(route.query.q ?? '');
  if (query.value) searchOpen.value = true;
  void refresh();
  void refreshCounts();
  const id = String(route.params.sourceId ?? '');
  if (id) { await loadMessage(id); return; }
  try {
    let sourceId = typeof route.query.source === 'string' ? route.query.source : '';
    if (typeof route.query.notice === 'string') {
      sourceId = (await getNotificationByNotice(route.query.notice)).id;
    } else if (typeof route.query.candidate === 'string') {
      sourceId = (await getInboxItem(route.query.candidate)).source?.id ?? '';
    }
    if (!disposed && token === selectToken && sourceId) {
      await router.replace({ ...listLocation(), path: '/inbox/' + encodeURIComponent(sourceId) });
    }
  } catch (failure) {
    if (token === selectToken) fail(failure);
  }
}

async function getNotificationByNotice(id: string): Promise<Message> {
  return apiRequest(`/api/v1/notifications/notice/${id}`);
}

watch(() => route.fullPath, () => { void syncRoute(); });

// 每次修改携带当前版本，冲突时刷新服务端状态，不覆盖较新的操作。
async function quickAction(item: Message, patch: { read?: boolean; mailbox?: Mailbox }) {
  if (disposed || actionId.value || busy.value) return;
  actionId.value = item.id;
  error.value = '';
  try {
    const state = await updateNotification(item.id, patch, item.version);
    if (disposed) return;
    // 废弃早于本次写入发出的列表请求，避免旧未读状态覆盖保存结果。
    listToken++; loading.value = false;
    items.value = items.value.map((current) => current.id === state.id ? state : current)
      .filter((current) => current.mailbox === mailbox.value);
    if (selected.value?.id === state.id) selected.value = { ...selected.value, ...state };
    void refreshCounts();
  } catch (failure) {
    fail(failure);
    await refresh();
  } finally {
    actionId.value = '';
  }
}

async function moveTo(next: Mailbox) {
  if (disposed || !selected.value || busy.value) return;
  const current = selected.value;
  const token = selectToken;
  busy.value = true;
  try {
    await updateNotification(current.id, { mailbox: next }, current.version);
    // 迟到的操作回执不能把用户从另一条通知或其他页面拉回列表。
    if (disposed || token !== selectToken) return;
    await closeMessage();
    await refresh();
    void refreshCounts();
  } catch (failure) {
    fail(failure);
  } finally {
    busy.value = false;
  }
}

async function markUnread() {
  if (disposed || !selected.value || busy.value) return;
  const current = selected.value;
  const token = selectToken;
  busy.value = true;
  try {
    await updateNotification(current.id, { read: false }, current.version);
    if (disposed || token !== selectToken) return;
    await closeMessage();
    await refresh();
    void refreshCounts();
  } catch (failure) {
    if (token === selectToken) fail(failure);
  } finally { busy.value = false; }
}

async function detachSource(sourceId: string) {
  if (!selected.value || busy.value) return;
  const current = selected.value;
  const token = selectToken;
  busy.value = true;
  error.value = '';
  try {
    await apiRequest(`/api/v1/notifications/${current.id}/detach/${sourceId}`, {
      method: 'POST',
      body: JSON.stringify({ expected_version: current.version }),
    });
    if (disposed || token !== selectToken) return;
    await closeMessage();
    status.value = '已拆开这条来源，后台不会自动将它合回去。可分别重新整理摘要。';
    selectToken += 1;
    await refresh();
    void refreshCounts();
  } catch (failure) {
    fail(failure);
  } finally {
    busy.value = false;
  }
}

async function organize() {
  if (!selected.value || busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    await apiRequest(`/api/v1/notifications/${selected.value.id}/organize`, { method: 'POST', body: '{}' });
    status.value = '主助手正在整理，结果会自动更新。';
  } catch (failure) {
    fail(failure);
  } finally {
    busy.value = false;
  }
}

function askMain() {
  window.dispatchEvent(new CustomEvent('open-main-agent', { detail: {
    context: selected.value ? `通知来源ID=${selected.value.id}；标题=${selected.value.title}` : '通知收件箱',
    prompt: selected.value ? '请帮我看看这条通知，给我处理建议。' : '',
  } }));
}

async function submitSource() {
  if (disposed || !text.value.trim() || busy.value) return;
  const token = selectToken;
  busy.value = true;
  error.value = '';
  try {
    const submittedText = text.value.trim();
    const submission = await sourceSubmission(auth.user?.id ?? '', { text: submittedText, mode: 'rules' });
    if (disposed || token !== selectToken) return;
    const result = await createTextSource(submittedText, undefined, { mode: 'rules' }, submission.key);
    submission.complete();
    if (disposed || token !== selectToken) return;
    text.value = '';
    composing.value = false;
    mailbox.value = 'inbox';
    await refresh();
    void refreshCounts();
    await openMessage(result.source.id);
  } catch (failure) {
    fail(failure);
  } finally {
    busy.value = false;
  }
}

function dateLabel(epoch: number) {
  return formatRelativeDateTime(epoch);
}

function ocrStatusLabel(status: string): string {
  return ({ queued: '排队中', leased: '已领取', running: '识别中', retry_wait: '等待重试',
    succeeded: '识别完成', failed: '识别失败', cancelled: '已取消' } as Record<string, string>)[status] ?? status;
}

function resetOcr(): void {
  if (ocrImageUrl.value.startsWith('blob:')) URL.revokeObjectURL(ocrImageUrl.value);
  ocrOpen.value = false;
  ocrFile.value = null;
  ocrImageUrl.value = '';
  ocrResult.value = null;
  ocrItems.value = [];
  ocrArtifact.value = null;
  ocrTitle.value = '';
  ocrProgress.value = null;
  ocrError.value = '';
}

function openOcrPicker(): void {
  if (busy.value || ocrBusy.value) return;
  ocrOpen.value = true;
  ocrInput.value?.click();
}

function setOcrFile(file?: File): void {
  if (!file) return;
  if (!['image/png', 'image/jpeg'].includes(file.type)) {
    ocrError.value = '目前只支持 PNG 或 JPEG 截图。';
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    ocrError.value = '图片超过 20 MiB，请先缩小后重试。';
    return;
  }
  if (ocrImageUrl.value.startsWith('blob:')) URL.revokeObjectURL(ocrImageUrl.value);
  ocrFile.value = file;
  ocrImageUrl.value = URL.createObjectURL(file);
  ocrResult.value = null;
  ocrArtifact.value = null;
  ocrItems.value = [];
  ocrError.value = '';
  ocrProgress.value = null;
  ocrOpen.value = true;
  void recognizeSelectedImage();
}

function handleOcrFileChange(event: Event): void {
  const input = event.target;
  if (input instanceof HTMLInputElement) setOcrFile(input.files?.[0]);
  if (input instanceof HTMLInputElement) input.value = '';
}

function handleImageDrop(event: DragEvent): void {
  event.preventDefault();
  setOcrFile(event.dataTransfer?.files[0]);
}

function handleClipboardPaste(event: ClipboardEvent): void {
  if (!ocrOpen.value) return;
  const file = [...(event.clipboardData?.items ?? [])]
    .find((item) => item.kind === 'file' && item.type.startsWith('image/'))
    ?.getAsFile();
  if (!file) return;
  event.preventDefault();
  setOcrFile(file);
}

async function recognizeSelectedImage(): Promise<void> {
  if (!ocrFile.value || ocrBusy.value) return;
  const file = ocrFile.value;
  const token = selectToken;
  ocrResult.value = null;
  ocrItems.value = [];
  ocrArtifact.value = null;
  ocrBusy.value = true;
  ocrError.value = '';
  ocrProgress.value = { phase: '开始校验图片', percent: 0 };
  try {
    if (ocrLocation.value === 'browser') {
      const result = await recognizeImage(
        file,
        ocrModel.value,
        'browser',
        ((detail: OcrProgress) => { ocrProgress.value = detail; }) as OcrProgressHandler,
      );
      if (disposed || token !== selectToken) return;
      const saved = await submitBrowserOcr(file, result);
      ocrArtifact.value = saved.artifact;
      ocrResult.value = result;
      // 解除 Vue 代理后再复制，避免后续校正提交触发 DataCloneError。
      ocrItems.value = structuredClone(toRaw(result.items));
      ocrProgress.value = { phase: '本地识别结果与私有原图已保存', percent: 100 };
      return;
    }
    const queued = await submitServerOcr(file);
    if (disposed || token !== selectToken) return;
    ocrArtifact.value = queued.artifact;
    ocrProgress.value = { phase: '服务器 OCR 已排队', indeterminate: true };
    const deadline = Date.now() + 5 * 60 * 1000;
    let latest = queued.artifact;
    while (Date.now() < deadline && !disposed && token === selectToken
      && ['queued', 'leased', 'running', 'retry_wait'].includes(latest.status)) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (disposed || token !== selectToken) return;
      try {
        latest = await getOcrArtifact(queued.artifact.id);
      } catch {
        continue; // 单次查询抖动不改状态，下一轮继续。
      }
      ocrArtifact.value = latest;
      ocrProgress.value = { phase: `服务器 OCR：${ocrStatusLabel(latest.status)}`, indeterminate: latest.status !== 'succeeded' };
    }
    if (disposed || token !== selectToken) return;
    if (latest.status !== 'succeeded' || !latest.result) {
      const diagnosis: Record<string, string> = {
        ocr_browser_missing: '服务器尚未安装 OCR 浏览器，请安装 Playwright Chromium 或配置 OCR_CHROMIUM_EXECUTABLE 后重试。',
        ocr_assets_missing: '服务器缺少 OCR 资源，请重新安装完整发行包。',
        ocr_runtime_failed: '服务器 OCR 运行失败，请检查浏览器与模型资源；也可以切换到浏览器本机识别。',
      };
      throw new Error(latest.status === 'failed'
        ? (diagnosis[latest.failure_code ?? ''] ?? `服务器 OCR 失败（${latest.failure_code ?? 'unknown'}）。`)
        : latest.status === 'cancelled' ? '服务器 OCR 已取消，可重新识别。' : '服务器 OCR 仍在运行，请稍后重试。');
    }
    ocrResult.value = latest.result;
    ocrItems.value = structuredClone(toRaw(latest.result.items));
    ocrProgress.value = { phase: '服务器识别完成，可校正文字与图框', percent: 100 };
  } catch (failure) {
    ocrError.value = failure instanceof ApiError ? failure.message
      : failure instanceof Error ? failure.message : 'OCR 失败，请重试或切换执行位置。';
  } finally {
    ocrBusy.value = false;
  }
}

async function saveOcrSource(): Promise<void> {
  if (!ocrArtifact.value || !ocrResult.value || ocrBusy.value) return;
  ocrBusy.value = true;
  ocrError.value = '';
  try {
    const created = await createOcrSource(
      ocrArtifact.value,
      ocrTitle.value.trim(),
      ocrItems.value,
      { mode: 'rules' },
    );
    if (disposed) return;
    text.value = '';
    composing.value = false;
    resetOcr();
    mailbox.value = 'inbox';
    await refresh();
    void refreshCounts();
    await openMessage(created.source.id);
  } catch (failure) {
    ocrError.value = failure instanceof ApiError ? failure.message
      : failure instanceof Error ? failure.message : '保存 OCR 来源失败。';
  } finally {
    ocrBusy.value = false;
  }
}

onMounted(async () => {
  await syncRoute();
  if (disposed) return;
  timer = setInterval(async () => {
    if (disposed || busy.value || loading.value || document.visibilityState === 'hidden') return;
    await refresh();
    void refreshCounts();
    if (!disposed && selected.value) {
      const id = selected.value.id;
      const token = selectToken;
      try {
        const detail = await getNotification(id);
        if (!disposed && token === selectToken && selected.value?.id === id && (detail.id !== id || detail.version >= selected.value.version)) selected.value = detail;
      } catch { /* 定时更新失败不打断阅读。 */ }
    }
  }, 10000);
});

onBeforeUnmount(() => {
  disposed = true;
  selectToken += 1;
  clearInterval(timer);
  if (ocrImageUrl.value.startsWith('blob:')) URL.revokeObjectURL(ocrImageUrl.value);
});
</script>

<template>
  <section class="page-section notifications-page">
    <header class="notification-heading">
      <h1>通知</h1>
      <div class="notification-actions">
        <mdui-button-icon v-if="!selected" aria-label="打开通知搜索" title="搜索通知"
          :aria-expanded="searchOpen" @click="openNotificationSearch">
          <AppIcon name="search" />
        </mdui-button-icon>
        <mdui-button-icon aria-label="打开资料库" title="资料库" @click="router.push('/knowledge')">
          <AppIcon name="description" />
        </mdui-button-icon>
        <mdui-button variant="outlined" @click="composing = !composing">添加通知</mdui-button>
      </div>
    </header>

    <AgentRuntimeAlert compact />

    <form v-if="composing" class="notification-composer" @submit.prevent="submitSource">
      <p v-if="sharedDraft" class="helper-text">已接收分享内容，请核对原文后再提交。</p>
      <mdui-text-field
        label="通知原文"
        :value="text"
        rows="5"
        autosize
        max-rows="12"
        maxlength="100000"
        required
        placeholder="粘贴原文即可，不必填写日期或结构化字段。"
        @input="text = readMduiValue($event)"
      ></mdui-text-field>
      <div class="notification-composer-actions">
        <mdui-button type="submit" variant="filled" :disabled="busy || !text.trim()">交给主助手整理</mdui-button>
        <mdui-button type="button" variant="outlined" :disabled="busy || ocrBusy" @click="openOcrPicker">OCR 识别</mdui-button>
        <RouterLink to="/agent" class="app-link">上传截图、文件或拍照给助手</RouterLink>
        <input ref="ocrInput" class="ocr-file-input" type="file" accept="image/png,image/jpeg" @change="handleOcrFileChange" />
      </div>
      <section
        v-if="ocrOpen"
        class="ocr-import"
        aria-labelledby="notification-ocr-heading"
        @paste="handleClipboardPaste"
      >
        <div class="ocr-import-heading">
          <div>
            <h3 id="notification-ocr-heading">截图 OCR</h3>
            <p class="helper-text">选择或拖入 PNG/JPEG，识别后可逐行修正，再保存为通知。</p>
          </div>
          <mdui-button type="button" variant="text" :disabled="ocrBusy" @click="resetOcr">收起</mdui-button>
        </div>
        <div class="ocr-drop-zone" @dragover.prevent @drop="handleImageDrop">
          <label>
            选择截图
            <input type="file" accept="image/png,image/jpeg" @change="handleOcrFileChange" />
          </label>
          <span>也可以把图片拖到这里，或按 Ctrl+V 粘贴截图</span>
        </div>
        <div class="ocr-controls">
          <label>执行位置
            <select v-model="ocrLocation" :disabled="ocrBusy">
              <option value="browser">浏览器本机 Worker</option>
              <option value="server">服务器 Chromium 队列</option>
            </select>
          </label>
          <label>识别模型
            <select v-model="ocrModel" :disabled="ocrBusy || ocrLocation === 'server'">
              <option value="tiny">PP-OCRv6 tiny（默认）</option>
              <option value="small">PP-OCRv6 small</option>
            </select>
          </label>
          <mdui-button type="button" variant="outlined" :disabled="ocrBusy || !ocrFile" @click="recognizeSelectedImage">
            重新识别
          </mdui-button>
        </div>
        <p v-if="ocrProgress" class="ocr-progress" role="status">
          <span>{{ ocrProgress.phase }}</span>
          <progress v-if="!ocrProgress.indeterminate && ocrProgress.percent !== undefined" :value="ocrProgress.percent" max="100"></progress>
          <mdui-circular-progress v-else></mdui-circular-progress>
        </p>
        <p v-if="ocrError" class="error-text inline-message" role="alert">{{ ocrError }}</p>
        <figure v-if="ocrImageUrl" class="ocr-image-stage">
          <img :src="ocrImageUrl" alt="待识别或已保存的通知截图" />
          <svg v-if="ocrResult" :viewBox="`0 0 ${ocrResult.width} ${ocrResult.height}`" preserveAspectRatio="none" aria-label="OCR 图框">
            <polygon v-for="item in ocrItems" :key="item.id" :points="item.poly.map((point) => point.join(',')).join(' ')" />
          </svg>
        </figure>
        <div v-if="ocrResult" class="ocr-result-panel">
          <div class="ocr-result-summary">
            <strong>{{ ocrResult.model_id }}</strong>
            <span>{{ ocrResult.width }} × {{ ocrResult.height }} · {{ ocrResult.elapsed_ms }} ms · {{ ocrResult.execution_location }}</span>
          </div>
          <p v-for="warning in ocrResult.warnings" :key="warning" class="warning-text">{{ warning }}</p>
          <label v-for="item in ocrItems" :key="item.id" class="ocr-correction-row">
            <span>图框 {{ item.id }} · {{ item.score === null ? '无分数' : `${Math.round(item.score * 100)}%` }}</span>
            <textarea v-model="item.text" rows="2" maxlength="10000"></textarea>
          </label>
          <label class="ocr-title-field">通知标题（可选）<input v-model="ocrTitle" maxlength="500" /></label>
          <div class="ocr-controls">
            <mdui-button
              type="button"
              variant="filled"
              :loading="ocrBusy"
              :disabled="ocrBusy || !ocrArtifact || !ocrItems.some((item) => item.text.trim())"
              @click="saveOcrSource"
            >
              保存并打开通知
            </mdui-button>
          </div>
          <p class="helper-text">图框与原始识别结果会保留；你编辑的文字作为新的来源片段保存。</p>
        </div>
      </section>
    </form>

    <p v-if="error" class="error-text inline-message" role="alert">{{ error }}</p>
    <p v-if="status" class="helper-text" role="status">{{ status }}</p>

    <template v-if="!selected">
      <div class="notification-controls">
        <div v-if="searchOpen" class="notification-search">
          <mdui-text-field
            ref="searchField"
            class="notification-search-field"
            type="search"
            variant="filled"
            clearable
            :value="query"
            placeholder="搜索标题、摘要或原文"
            aria-label="搜索通知"
            @input="query = readMduiValue($event)"
            @keydown.enter.prevent="searchMessages"
          ></mdui-text-field>
          <mdui-button-icon aria-label="搜索" :disabled="loading" @click="searchMessages">
            <AppIcon name="search" />
          </mdui-button-icon>
          <mdui-button-icon aria-label="收起搜索" @click="searchOpen = false; query = ''; searchMessages()">
            <AppIcon name="close" />
          </mdui-button-icon>
        </div>
        <div class="mailbox-filters" role="group" aria-label="通知分组">
          <mdui-button
            v-for="option in mailboxOptions"
            :key="option.value"
            variant="text"
            :aria-pressed="mailbox === option.value"
            @click="changeMailbox(option.value)"
          >
            <AppIcon v-if="mailbox === option.value" slot="icon" name="check" />
            {{ option.label }}<span class="mailbox-count">{{ counts[option.value] }}</span>
          </mdui-button>
          <mdui-button-icon class="mailbox-refresh" aria-label="刷新通知" :disabled="loading"
            @click="refresh(); refreshCounts()">
            <AppIcon name="refresh" />
          </mdui-button-icon>
        </div>
      </div>

      <div class="notification-message-list" :aria-busy="loading">
        <mdui-list>
          <mdui-list-item
            v-for="item in items"
            :key="item.id"
            class="notification-message"
            :data-source-id="item.id"
            :class="{ 'notification-unread': !item.read }"
            :description-line="2"
            @click="openMessage(item.id)"
          >
            <span slot="icon" class="notification-row-marker" :data-unread="!item.read"
              :aria-label="item.read ? undefined : '未读'" :aria-hidden="item.read || undefined">
              <span class="notification-read-dot"></span>
            </span>
            <!-- 两个页面共用已读样式，已读通知不再保留灰点等状态标识。 -->
            <span class="notification-title" :data-read="item.read">{{ item.title }}</span>
            <span slot="description" class="message-summary">
              {{ item.summary || '尚未整理，点击查看通知原文。' }}
            </span>
            <span slot="end-icon" class="message-end">
              <time>{{ dateLabel(item.created_at_epoch) }}</time>
              <span v-if="(item.source_count ?? 1) > 1" class="message-source-count">汇总 {{ item.source_count }} 条</span>
              <!-- 常用操作收入菜单，让标题和摘要有足够宽度；菜单事件不冒泡到消息行。 -->
              <mdui-dropdown class="message-quick-actions" placement="bottom-end" @click.stop @keydown.stop>
                <mdui-button-icon slot="trigger" aria-label="通知操作" :disabled="Boolean(actionId)">
                  <AppIcon name="more_vert" />
                </mdui-button-icon>
                <mdui-menu>
                  <mdui-menu-item @click="quickAction(item, { mailbox: item.mailbox === 'archived' ? 'inbox' : 'archived' })">
                    {{ item.mailbox === 'archived' ? '移回收件箱' : '归档' }}
                  </mdui-menu-item>
                  <mdui-menu-item @click="quickAction(item, { read: !item.read })">
                    {{ item.read ? '标未读' : '已读' }}
                  </mdui-menu-item>
                  <mdui-menu-item @click="quickAction(item, { mailbox: item.mailbox === 'ignored' ? 'inbox' : 'ignored' })">
                    {{ item.mailbox === 'ignored' ? '移回收件箱' : '忽略' }}
                  </mdui-menu-item>
                </mdui-menu>
              </mdui-dropdown>
            </span>
          </mdui-list-item>
        </mdui-list>
        <p v-if="!items.length" class="helper-text notification-empty">
          {{ loading ? '正在读取通知……' : '这个分组暂时没有通知。' }}
        </p>
        <mdui-button v-if="hasMore" variant="outlined" :disabled="loading" @click="refresh(true)">加载更多</mdui-button>
      </div>
    </template>

    <article v-else class="notification-reader">
      <mdui-button variant="text" @click="closeMessage">
        <AppIcon slot="icon" name="chevron_left"></AppIcon>
        返回列表
      </mdui-button>
      <h2>{{ selected.title }}</h2>
      <p v-if="selected.summary" class="notification-summary">{{ selected.summary }}</p>
      <section v-if="selected.advice" class="notification-advice">
        <h3>主助手建议</h3>
        <p>{{ selected.advice }}</p>
      </section>
      <div class="notification-actions">
        <mdui-button variant="filled" @click="askMain">就这条通知聊聊</mdui-button>
        <mdui-button variant="text" :disabled="busy" @click="organize">{{ selected.organized ? '重新整理' : '整理标题和摘要' }}</mdui-button>
      </div>
      <section v-if="selected.sources?.length" class="notification-original">
        <h3>通知原文 <small v-if="selected.sources.length > 1">（{{ selected.sources.length }} 条来源）</small></h3>
        <details v-for="source in selected.sources" :key="source.id" :open="selected.sources.length === 1" class="notification-source">
          <summary>{{ source.title }} · {{ dateLabel(source.created_at_epoch) }}</summary>
          <pre>{{ source.original_text }}</pre>
          <CollectedAttachments :source-id="source.id" />
          <mdui-button v-if="selected.sources.length > 1" variant="text" :disabled="busy" @click="detachSource(source.id)">这条是独立通知，拆开</mdui-button>
        </details>
      </section>
      <section v-else class="notification-original">
        <h3>通知原文</h3>
        <pre>{{ selected.original_text }}</pre>
        <CollectedAttachments :key="`attachments:${selected.id}`" :source-id="selected.id" />
      </section>
      <NoticeEvidence v-if="selected.notices?.length" :key="selected.id" :notices="selected.notices" />
      <p v-if="selected.notices_has_more" class="helper-text">当前显示前 50 条业务通知索引，可让主助手继续检索。</p>
      <div class="notification-actions notification-detail-actions">
        <mdui-button v-if="selected.mailbox !== 'archived'" variant="text" :disabled="busy" @click="moveTo('archived')">归档</mdui-button>
        <mdui-button v-if="selected.mailbox !== 'ignored'" variant="text" :disabled="busy" @click="moveTo('ignored')">忽略</mdui-button>
        <mdui-button v-if="selected.mailbox !== 'inbox'" variant="text" :disabled="busy" @click="moveTo('inbox')">移回收件箱</mdui-button>
        <mdui-button variant="text" @click="markUnread">标为未读</mdui-button>
      </div>
      <details class="notification-records">
        <summary>处理记录</summary>
        <p>{{ selected.organized ? '主助手已整理标题、摘要与建议。' : '暂未生成整理结果，当前使用原始标题。' }}</p>
        <p v-if="selected.change_set">{{ selected.change_set.summary }}</p>
        <small>来源 {{ selected.source_type }} · 版本 {{ selected.version }}</small>
      </details>
    </article>
  </section>
</template>

<style scoped>
.notifications-page { max-width: 1080px; }
.notification-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.notification-heading h1 { margin: 0; font-size: clamp(2rem, 4vw, 2.75rem); line-height: 1.18; }
.notification-actions { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.notification-controls { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 12px; margin: 12px 0 6px; }
.mailbox-filters { display: flex; flex-wrap: wrap; gap: 4px; }
.mailbox-filters mdui-button { --mdui-color-primary: var(--mdui-color-on-surface-variant); }
.mailbox-filters mdui-button[aria-pressed='true'] {
  background: rgb(var(--mdui-color-secondary-container));
  --mdui-color-primary: var(--mdui-color-on-secondary-container);
}
.mailbox-count {
  margin-left: 6px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.notification-composer { display: grid; gap: 8px; margin: 12px 0; padding: 12px; border: 1px solid var(--app-outline, #dfe3ec); border-radius: 14px; background: var(--app-surface-container, #f7f8fc); }
.notification-composer-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ocr-file-input { display: none; }
.notification-search { display: flex; align-items: center; flex: 0 1 340px; gap: 2px; margin: 0; padding: 0; background: transparent; }
.notification-search-field { flex: 1; min-width: 0; }
.notification-message-list { display: grid; gap: 6px; }
.notification-message-list mdui-list-item { min-height: 60px; --mdui-typescale-label-large-size: 15px; }
.notification-message .notification-title { font-size: 15px; line-height: 1.4; }
.notification-unread { background: color-mix(in srgb, var(--mdui-color-primary, 49 93 168) 4%, transparent); }
.message-summary { display: -webkit-box; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; color: var(--app-on-surface-variant, #667080); font-size: 13px; line-height: 1.45; }
.message-end { display: flex; min-width: 44px; flex-direction: column; align-items: flex-end; gap: 2px; }
.message-end time { color: var(--app-on-surface-variant, #7a8190); font-size: 11px; white-space: nowrap; }
.message-source-count { color: var(--mdui-color-primary, #315da8); font-size: 9px; }
.message-quick-actions mdui-button-icon { width: 36px; height: 36px; }
.notification-empty { padding: 24px 8px; text-align: center; }
.notification-reader { max-width: 900px; }
.notification-reader h2 { margin: 12px 0 10px; font-size: clamp(1.35rem, 3vw, 1.9rem); line-height: 1.4; overflow-wrap: anywhere; }
.notification-summary, .notification-advice p { white-space: pre-wrap; line-height: 1.8; }
.notification-advice { margin: 14px 0; padding: 10px 14px; border-left: 3px solid color-mix(in srgb, var(--mdui-color-primary, 49 93 168) 45%, transparent); border-radius: 0 10px 10px 0; background: var(--app-surface-container, #f5f7fc); }
.notification-original { margin: 22px 0; }
.notification-original pre { white-space: pre-wrap; font: inherit; line-height: 1.8; overflow-wrap: anywhere; }
.notification-source { margin: 8px 0; padding: 8px 10px; border: 1px solid var(--app-outline-variant, #e4e7ef); border-radius: 10px; }
.notification-source summary { cursor: pointer; color: var(--app-on-surface-variant, #596275); }
.notification-detail-actions { margin-top: 18px; padding-top: 10px; border-top: 1px solid var(--app-outline-variant, #e4e7ef); }
.notification-records { margin-top: 18px; color: var(--app-on-surface-variant, #667080); font-size: 11px; }
@media (max-width: 700px) {
  .notification-actions { justify-content: flex-end; }
  .notification-search { flex: 1 1 100%; }
}
</style>
