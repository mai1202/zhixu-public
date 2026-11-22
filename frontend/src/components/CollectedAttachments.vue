<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { ApiError } from '../api/client';
import { listCollectedAttachments, retryCollectedAttachment, type CollectedAttachment } from '../api/devices';

const props = defineProps<{ sourceId?: string }>();
const items = ref<CollectedAttachment[]>([]);
const error = ref('');
const loading = ref(false);
const busyId = ref('');
let disposed = false;
let version = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const statusLabels: Record<string, string> = { receiving: '正在保存', queued: '等待解析', parsing: '正在解析', ocr_pending: '图片识别中',
  retry_wait: '等待重试', ready: '附件已保存', converted: '已转为通知', unavailable: '暂不可读取', failed: '处理未完成' };
const errorLabels: Record<string, string> = { not_downloaded: '文件尚未下载到本机微信，请下载后重新同步。',
  identity_unavailable: '消息缺少精确文件标识，未猜测上传。', unsupported_format: '此文件格式或图片加密方式暂不支持，可手动上传导出的文件。',
  hash_mismatch: '文件内容与消息标识不一致，已停止上传。', too_large: '文件超过 10 MiB，请拆分或手动处理。',
  path_rejected: '附件位置无法通过核验。', attachment_parse_failed: '文件解析失败，原件已保留，可下载检查后重试。',
  attachment_ocr_pending: '正在等待图片识别完成。', attachments_disabled: '该设备附件采集已关闭。', chat_not_selected: '原群已从采集范围排除。',
  attachment_ocr_failed: '图片识别失败，原图已保留。请检查服务端 OCR 运行环境后重试。',
  attachment_no_text: '没有可提取的文本，扫描 PDF 可转成图片再上传。', attachment_text_limit: '文本超过单条通知限制，请拆分处理。' };

async function load(background = false): Promise<void> {
  if (disposed) return;
  const request = ++version;
  if (!background) { loading.value = true; error.value = ''; }
  try {
    const result = await listCollectedAttachments(props.sourceId);
    if (!disposed && request === version) items.value = result.items;
  } catch (failure) {
    if (!disposed && request === version && !background) error.value = failure instanceof ApiError ? failure.message : '附件记录暂时无法加载。';
  } finally {
    if (!disposed && request === version) loading.value = false;
  }
}

async function poll(): Promise<void> {
  await load(true);
  if (!disposed) timer = setTimeout(() => { void poll(); }, 5000);
}

async function retry(item: CollectedAttachment): Promise<void> {
  if (busyId.value) return;
  busyId.value = item.id;
  error.value = '';
  try {
    await retryCollectedAttachment(item.id);
    await load();
  } catch (failure) {
    if (!disposed) error.value = failure instanceof ApiError ? failure.message : '附件重试未能启动。';
  } finally { if (!disposed) busyId.value = ''; }
}

watch(() => props.sourceId, () => { items.value = []; void load(); });
onMounted(() => { void load(); timer = setTimeout(() => { void poll(); }, 5000); });
onUnmounted(() => { disposed = true; version += 1; clearTimeout(timer); });
</script>

<template>
  <section v-if="!sourceId || items.length || error" class="collected-attachments" aria-label="已采集附件">
    <header><div><h3>已采集附件</h3><p>原文上传与附件处理分别确认，失败不会伪装成已转化。</p></div><mdui-button variant="text" :disabled="loading" @click="load()">刷新附件记录</mdui-button></header>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <p v-if="loading && !items.length" role="status">正在读取附件……</p>
    <ul v-if="items.length">
      <li v-for="item in items" :key="item.id">
        <div><strong>{{ item.filename }}</strong><span>{{ statusLabels[item.status] ?? item.status }}<template v-if="item.size_bytes"> · {{ (item.size_bytes / 1024).toFixed(0) }} KB</template></span></div>
        <p v-if="item.error_code">{{ errorLabels[item.error_code] ?? '处理暂未完成，可查看原件或重新尝试。' }}</p>
        <nav aria-label="附件操作">
          <a v-if="item.download_href" :href="item.download_href">下载原附件</a>
          <RouterLink v-if="item.source_document_id" :to="{ path: '/inbox', query: { source: item.source_document_id } }">查看转化的通知</RouterLink>
          <RouterLink v-if="!sourceId" :to="{ path: '/inbox', query: { source: item.parent_source_id } }">查看原群消息</RouterLink>
          <mdui-button v-if="item.status === 'failed'" variant="text" :disabled="Boolean(busyId)" @click="retry(item)">{{ busyId === item.id ? '正在重试' : '重试解析' }}</mdui-button>
        </nav>
      </li>
    </ul>
    <p v-else-if="!loading" class="helper-text">还没有附件记录。设备开启附件采集后，可在已接管群发送图片或支持的文档，并点击立即同步。</p>
  </section>
</template>

<style scoped>
/* 附件状态和操作使用清晰行布局，长文件名在手机端也不会撑宽页面。 */
.collected-attachments { padding: 20px; border: 1px solid var(--app-outline, #ddd); border-radius: 16px; margin: 20px 0; overflow-wrap: anywhere; }
header, li > div, nav { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
h3 { margin: 0; }
p, span { color: var(--app-on-surface-variant); font-size: 13px; line-height: 1.7; }
ul { padding: 0; list-style: none; }
li { padding: 16px 0; border-top: 1px solid var(--app-outline, #ddd); }
nav { justify-content: flex-start; }
</style>
