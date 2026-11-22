<script setup lang="ts">
/** 按路线挂载独立组件，切换路线后不显示上一个协作空间的内容。 */
import { onMounted, onUnmounted, ref } from 'vue';
import { ApiError } from '../api/client';
import { uploadAgentAttachment } from '../api/agent';
import { getCollaboration, postComment, submitFile, removeCollaboration, getAvailabilityConsent,
  saveAvailabilityConsent, getCommonAvailability, type CollaborationContent, type AvailabilityConsent,
  type CommonAvailability } from '../api/collaboration';

const props = defineProps<{ journeyId: string }>();
const content = ref<CollaborationContent>({ comments: [], files: [] });
const consent = ref<AvailabilityConsent | null>(null);
const common = ref<CommonAvailability | null>(null);
const message = ref('');
const audience = ref<'owner' | 'members'>('owner');
const file = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const busy = ref(false);
const error = ref('');
let disposed = false;
onUnmounted(() => { disposed = true; });
onMounted(() => void run(async () => {
  const [data, state] = await Promise.all([getCollaboration(props.journeyId), getAvailabilityConsent(props.journeyId)]);
  if (!disposed) { content.value = data; consent.value = state; }
}));

async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { await action(); }
  catch (failure) { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '操作未完成，请稍后重试。'; }
  finally { if (!disposed) busy.value = false; }
}
function comment(): void {
  void run(async () => {
    const data = await postComment(props.journeyId, message.value);
    if (!disposed) { content.value = data; message.value = ''; }
  });
}
function upload(): void {
  const selected = file.value;
  if (!selected) return;
  void run(async () => {
    const result = await uploadAgentAttachment(selected);
    const data = await submitFile(props.journeyId, result.attachment.id, audience.value);
    if (!disposed) { content.value = data; file.value = null; if (fileInput.value) fileInput.value.value = ''; }
  });
}
function remove(kind: 'comments' | 'files', id: string): void {
  void run(async () => { const data = await removeCollaboration(props.journeyId, kind, id); if (!disposed) content.value = data; });
}
function authorize(enabled: boolean): void {
  const state = consent.value;
  if (!state) return;
  void run(async () => {
    const result = await saveAvailabilityConsent(props.journeyId, state, enabled);
    if (!disposed) { consent.value = result; common.value = null; }
  });
}
function findCommon(): void {
  void run(async () => { const result = await getCommonAvailability(props.journeyId); if (!disposed) common.value = result; });
}
function chooseFile(event: Event): void { file.value = (event.target as HTMLInputElement).files?.[0] ?? null; }
function time(value: string): string { return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
</script>

<template>
  <section class="collaboration" aria-label="路线协作">
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <h2>留言与文件</h2>
    <form @submit.prevent="comment">
      <label>给成员留言<textarea v-model="message" required maxlength="2000" rows="3"></textarea></label>
      <mdui-button type="submit" :disabled="busy || !message.trim()">发布留言</mdui-button>
    </form>
    <ul class="content-list"><li v-for="item in content.comments" :key="item.id">
      <strong>{{ item.author }}</strong><p class="comment-text">{{ item.content }}</p>
      <mdui-button v-if="item.can_delete" variant="text" :disabled="busy" @click="remove('comments', item.id)">撤回留言</mdui-button>
    </li></ul>
    <form @submit.prevent="upload">
      <label>提交文件（不超过 10 MB）<input ref="fileInput" type="file" required @change="chooseFile" /></label>
      <label>文件可见范围<select v-model="audience"><option value="owner">仅发起人和我</option><option value="members">所有协作成员</option></select></label>
      <mdui-button type="submit" :disabled="busy || !file">提交文件</mdui-button>
    </form>
    <ul class="content-list"><li v-for="item in content.files" :key="item.id">
      <a :href="item.download_url" download>{{ item.filename }}</a>
      <p>{{ item.author }} · {{ item.audience === 'owner' ? '仅发起人和上传者可见' : '成员可见' }}</p>
      <mdui-button v-if="item.can_delete" variant="text" :disabled="busy" @click="remove('files', item.id)">撤回文件</mdui-button>
    </li></ul>
    <h2>共同空闲时间</h2>
    <p>每位成员单独授权后，结合各自课表、日程查找未来七天的共同时间。成员只能查看交集。</p>
    <template v-if="consent">
      <label>我的时区<input v-model="consent.availability.timezone" :disabled="busy" /></label>
      <div class="window"><label>开始时间<input v-model="consent.availability.start_time" type="time" :disabled="busy" /></label>
        <label>结束时间<input v-model="consent.availability.end_time" type="time" :disabled="busy" /></label></div>
      <fieldset :disabled="busy"><legend>可用星期</legend><label v-for="day in [1,2,3,4,5,6,7]" :key="day" class="weekday">
        <input v-model="consent.availability.weekdays" type="checkbox" :value="day" />{{ ['一','二','三','四','五','六','日'][day-1] }}</label></fieldset>
      <p v-if="consent.enabled">已授权，有效期至 {{ new Date((consent.expires_at_epoch ?? 0) * 1000).toLocaleString('zh-CN') }}。</p>
      <div class="actions">
        <mdui-button :disabled="busy" @click="authorize(true)">{{ consent.enabled ? '更新窗口并续期七天' : '同意参与统筹（七天）' }}</mdui-button>
        <mdui-button v-if="consent.enabled" variant="text" :disabled="busy" @click="authorize(false)">撤回授权</mdui-button>
        <mdui-button variant="outlined" :disabled="busy" @click="findCommon">查找共同时间</mdui-button>
      </div>
    </template>
    <template v-if="common">
      <p v-if="common.status === 'awaiting_consent'">{{ common.consented }}/{{ common.participants }} 人已授权，等待全部成员同意。</p>
      <template v-else><p v-if="!common.slots.length">授权窗口内暂无半小时以上的共同时间。</p>
        <ul><li v-for="slot in common.slots" :key="slot.start_at">{{ time(slot.start_at) }} — {{ time(slot.end_at) }}</li></ul></template>
    </template>
  </section>
</template>

<style scoped>
.collaboration{padding:22px;border:1px solid var(--border-color,#ddd);border-radius:18px;overflow-wrap:anywhere}
h2{font-size:1.2rem}label{display:block;margin:10px 0}input:not([type=checkbox]),textarea,select{display:block;box-sizing:border-box;width:100%;padding:10px;border:1px solid #aaa;border-radius:8px;background:transparent;color:inherit;font:inherit}
.window,.actions{display:flex;gap:12px;flex-wrap:wrap}.window>label{flex:1;min-width:140px}fieldset{border:0;padding:0}.weekday{display:inline-flex;gap:4px;margin-right:14px}.content-list{padding:0;list-style:none}.content-list li{border-bottom:1px solid #ddd;padding:12px 0}.comment-text{white-space:pre-wrap}.actions{margin:12px 0}
</style>
