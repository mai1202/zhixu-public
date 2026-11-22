<script setup lang="ts">
/** 低置信度建议先展示依据，确认前不写入正式事务或日程。 */
import { onMounted, onUnmounted, ref } from 'vue';
import { apiRequest, ApiError } from '../api/client';
interface EventDraft { title: string; actor?: string | null; stage?: string | null; scope?: string | null; deadline_date: string | null; deadline_time: string | null; deadline_raw?: string | null; materials: string[]; submission?: string | null; materials_text: string }
interface Suggestion { id: string; version: number; source_id: string; source_title: string; source_text: string; reason: string; confidence: string; events: EventDraft[] }
const emit = defineEmits<{ changed: [] }>();
const items = ref<Suggestion[]>([]);
const error = ref('');
const busy = ref('');
let timer: ReturnType<typeof setInterval> | undefined;
let disposed = false;
async function refresh(): Promise<void> {
  if (busy.value) return;
  try {
    const data = await apiRequest<{ items: Suggestion[] }>('/api/v1/notification-suggestions');
    if (disposed) return;
    // 自动刷新保留正在编辑的草稿，同一版本不覆盖用户输入。
    items.value = data.items.map(item => items.value.find(old => old.id === item.id && old.version === item.version)
      ?? { ...item, events: (item.events.length ? item.events : [{ title: item.source_title, deadline_date: null, deadline_time: null, materials: [] }]).map(event => ({ ...event, materials_text: event.materials.join('\n') })) });
  } catch (failure) { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '待确认建议暂时未加载。'; }
}
onMounted(() => { void refresh(); timer = setInterval(() => { if (!document.hidden) void refresh(); }, 45000); });
onUnmounted(() => { disposed = true; if (timer) clearInterval(timer); });
async function decide(item: Suggestion, confirm: boolean): Promise<void> {
  if (busy.value) return;
  busy.value = item.id; error.value = '';
  try {
    const body = confirm ? { expected_version: item.version, events: item.events.map(event => ({
      title: event.title, actor: event.actor ?? null, stage: event.stage ?? null, scope: event.scope ?? null,
      deadline_date: event.deadline_date || null, deadline_time: event.deadline_date ? event.deadline_time || null : null,
      materials: event.materials_text.split('\n').map(value => value.trim()).filter(Boolean), submission: event.submission || null,
    })) } : { expected_version: item.version };
    await apiRequest(`/api/v1/notification-suggestions/${item.id}/${confirm ? 'confirm' : 'dismiss'}`, { method: 'POST', body: JSON.stringify(body) });
    if (!disposed) { items.value = items.value.filter(value => value.id !== item.id); emit('changed'); }
  } catch (failure) { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '操作未完成，请重试。'; }
  finally { if (!disposed) busy.value = ''; }
}
</script>
<template>
  <section v-if="items.length || error" class="notification-suggestions" aria-label="待确认建议">
    <h2>待确认建议</h2><p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <article v-for="item in items" :key="item.id">
      <h3>{{ item.source_title }}</h3><p>{{ item.reason }}</p><p class="confidence-label">解析把握度较低，尚未自动登记</p>
      <details><summary>对照通知原文</summary><pre>{{ item.source_text }}</pre><RouterLink :to="`/inbox/${item.source_id}`">打开通知详情</RouterLink></details>
      <form @submit.prevent="decide(item, true)">
        <fieldset v-for="(event, index) in item.events" :key="index" :disabled="Boolean(busy)">
          <legend>建议事项 {{ index + 1 }}</legend><label>事务标题<input v-model="event.title" required maxlength="500" /></label>
          <p v-if="event.deadline_raw">原截止表述：{{ event.deadline_raw }}</p>
          <div class="date-fields"><label>核对日期<input v-model="event.deadline_date" type="date" /></label><label>核对时刻（北京时间）<input v-model="event.deadline_time" type="time" :disabled="!event.deadline_date" /></label></div>
          <p v-if="event.actor || event.stage || event.scope">对象与阶段：{{ [event.actor, event.stage, event.scope].filter(Boolean).join(' · ') }}</p>
          <label>材料（每行一项）<textarea v-model="event.materials_text" rows="2"></textarea></label>
          <label>提交方式<input v-model="event.submission" maxlength="2000" /></label>
        </fieldset>
        <div class="actions"><mdui-button type="submit" :disabled="Boolean(busy)" :loading="busy === item.id">核对后登记事务</mdui-button><mdui-button type="button" variant="text" :disabled="Boolean(busy)" @click="decide(item, false)">暂不登记</mdui-button></div>
      </form>
    </article>
  </section>
</template>
<style scoped>
.notification-suggestions{padding:22px;border:1px solid var(--app-outline,#d6d9de);border-radius:18px;margin:20px 0;background:var(--app-surface-container,#f6f7f9)}h2{font-size:1.2rem}h3{font-size:1rem}article+article{border-top:1px solid #ddd;margin-top:24px;padding-top:16px}label{display:block;margin:10px 0}input,textarea{width:100%;box-sizing:border-box;padding:10px;border:1px solid #aaa;border-radius:8px;color:inherit;background:transparent;font:inherit}.date-fields,.actions{display:flex;gap:14px;flex-wrap:wrap}.date-fields label{flex:1;min-width:160px}fieldset{border:1px solid #ddd;border-radius:10px;margin:14px 0;min-width:0}pre{white-space:pre-wrap;max-height:360px;overflow:auto;overflow-wrap:anywhere}.confidence-label{color:var(--app-on-surface-variant,#666);font-size:13px}
</style>
