<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { apiRequest, ApiError } from '../api/client';
import { readMduiChecked, readMduiValue } from '../utils/mdui';

interface WatchDelta {
  meaningful?: boolean;
  count?: number;
  summary?: string;
}

interface Plan {
  id: string;
  title: string;
  message: string;
  mode: string;
  next_run_at_epoch: number;
  interval_seconds: number | null;
  status: string;
  version: number;
  last_run_id: string | null;
  last_success_epoch: number | null;
  last_error_code: string | null;
  last_watch_checked_epoch: number | null;
  last_notified_epoch: number | null;
  last_watch_delta_json: WatchDelta | null;
  watch_query: string | null;
  watch_scope: string;
  watch_url: string | null;
  run_count: number | null;
  stop_at_epoch: number | null;
  pending_run_id: string | null;
  pending_attempts: number | null;
  pending_since_epoch: number | null;
}

const plans = ref<Plan[]>([]);
const title = ref('');
const message = ref('');
const mode = ref('main');
const runAt = ref('');
const interval = ref(0);
const watchQuery = ref('');
const watchScope = ref('all');
const watchUrl = ref('');
const busy = ref(false);
const error = ref('');
const success = ref('');
const showForm = ref(false);
const showWatchForm = ref(false);
let submissionKey = crypto.randomUUID();
let attemptedPayload = '';

async function load() { plans.value = (await apiRequest<{items:Plan[]}>('/api/v1/agent/schedules')).items; }
function fail(failure: unknown) { error.value = failure instanceof ApiError ? failure.message : '计划操作失败。'; }
function clearMessages() { error.value = ''; success.value = ''; }
function statusLabel(status: string): string {
  return ({ active: '已启用', paused: '已暂停', completed: '已完成', cancelled: '已取消' } as Record<string, string>)[status] || status;
}
function dateTime(epoch: number | null): string {
  return epoch ? new Date(epoch * 1000).toLocaleString('zh-CN') : '尚未记录';
}
function minuteLocal(epoch: number): string {
  return new Date(epoch * 1000).toLocaleString('zh-CN', { hour12: false }).replace(/秒$/, '');
}
function onWatchPeriodChange(event: Event): void {
  setWatchPreset(readMduiValue(event));
}
function setWatchPreset(value: string): void {
  // 下拉框直接提交秒数；只接受界面提供的周期，避免无效值清空重复计划。
  const seconds = Number(value);
  if ([21600, 43200, 86400, 604800].includes(seconds)) interval.value = seconds;
}
watch(watchQuery, (value) => {
  // 关注必须重复检查；默认每天一次，用户仍可改为 6/12 小时。
  if (value.trim() && !interval.value) interval.value = 86400;
});
watch(showWatchForm, (value) => {
  // 关闭关注设置时清空关键词，避免普通一次任务被误建为持续关注。
  if (!value) { watchQuery.value = ''; watchUrl.value = ''; }
});

async function create() {
  busy.value = true;
  clearMessages();
  try {
    if (watchQuery.value.trim() && !interval.value) {
      error.value = '持续关注需要选择检查周期。';
      return;
    }
    // 本地日期输入转换为带时区的确定时刻，服务端不会猜测时区。
    const payload = { title: title.value, message: message.value, mode: mode.value,
      run_at: new Date(runAt.value).toISOString(), interval_seconds: interval.value || null,
      watch_query: watchQuery.value.trim() || null, watch_scope: watchScope.value,
      watch_url: watchUrl.value.trim() || null };
    const signature = JSON.stringify(payload);
    if (signature !== attemptedPayload) { submissionKey = crypto.randomUUID(); attemptedPayload = signature; }
    await apiRequest('/api/v1/agent/schedules', { method: 'POST', body: JSON.stringify({ ...payload, idempotency_key: submissionKey }) });
    submissionKey = crypto.randomUUID(); attemptedPayload = '';
    showForm.value = false; title.value = ''; message.value = ''; watchQuery.value = ''; watchUrl.value = ''; interval.value = 0;
    success.value = '计划已保存。关注任务只在出现有意义变化时唤醒主助手。';
    await load();
  } catch (failure) { fail(failure); }
  finally { busy.value = false; }
}

async function setStatus(plan: Plan, action: 'pause' | 'resume' | 'cancel') {
  busy.value = true;
  clearMessages();
  try {
    await apiRequest('/api/v1/agent/schedules/' + plan.id + '/status', {
      method: 'PATCH', body: JSON.stringify({ expected_version: plan.version, action }) });
    success.value = action === 'pause' ? '已暂停后续检查。' : action === 'resume' ? '已恢复检查。' : '计划已取消。';
    await load();
  } catch (failure) { fail(failure); }
  finally { busy.value = false; }
}

onMounted(() => { void load().catch(fail); });
</script>
<template>
  <section class="agent-task-plans" aria-labelledby="agent-task-plans-heading">
    <div class="plans-heading">
      <div>
        <h2 id="agent-task-plans-heading">定时任务与持续关注</h2>
        <p>可定时唤醒主助手，或保存关注范围后只在资料出现新增、日期/材料要求变化等有意义更新时提醒。也可以直接在聊天中让主助手设置。</p>
      </div>
      <mdui-button variant="outlined" @click="showForm = !showForm">创建计划</mdui-button>
    </div>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="success" class="success-text" role="status">{{ success }}</p>
    <form v-if="showForm" class="plans-form" @submit.prevent="create">
      <mdui-text-field label="计划名称" :value="title" required maxlength="150" @input="title = readMduiValue($event)"></mdui-text-field>
      <mdui-text-field label="届时执行的任务" :value="message" required rows="3" autosize max-rows="8" placeholder="例如：核对明天的课程和事务，把需要准备的材料发邮件给我。" @input="message = readMduiValue($event)"></mdui-text-field>
      <mdui-text-field label="执行时间" :value="runAt" required type="datetime-local" @input="runAt = readMduiValue($event)"></mdui-text-field>
      <mdui-select variant="outlined" label="会话" :value="mode" @change="mode = readMduiValue($event) as 'main' | 'child'">
        <mdui-menu-item value="main">唤醒固定主助手</mdui-menu-item>
        <mdui-menu-item value="child">新建普通子对话</mdui-menu-item>
      </mdui-select>
      <mdui-select variant="outlined" label="重复" :value="String(interval)" @change="interval = Number(readMduiValue($event)) || 0">
        <mdui-menu-item value="0">只执行一次</mdui-menu-item>
        <mdui-menu-item value="21600">每 6 小时</mdui-menu-item>
        <mdui-menu-item value="43200">每 12 小时</mdui-menu-item>
        <mdui-menu-item value="86400">每天</mdui-menu-item>
        <mdui-menu-item value="604800">每周</mdui-menu-item>
      </mdui-select>
      <mdui-checkbox class="checkbox-field" :checked="showWatchForm" @change="showWatchForm = readMduiChecked($event)">把它设为持续关注（只在有意义变化时提醒）</mdui-checkbox>
      <template v-if="showWatchForm">
        <mdui-text-field label="关注关键词" :value="watchQuery" maxlength="300" placeholder="例如：大挑报名、转专业规定" @input="watchQuery = readMduiValue($event)"></mdui-text-field>
        <mdui-select variant="outlined" label="关注范围" :value="watchScope" @change="watchScope = readMduiValue($event) as 'all' | 'private' | 'public'">
          <mdui-menu-item value="all">私人资料与公共库</mdui-menu-item>
          <mdui-menu-item value="private">只看私人资料</mdui-menu-item>
          <mdui-menu-item value="public">只看公共库</mdui-menu-item>
        </mdui-select>
        <mdui-text-field label="官网地址（可选）" type="url" :value="watchUrl" maxlength="2000" placeholder="https://www.gdufs.edu.cn/..." @input="watchUrl = readMduiValue($event)"></mdui-text-field>
        <mdui-select variant="outlined" label="检查周期" :value="String(interval)" @change="onWatchPeriodChange">
          <mdui-menu-item value="21600">每 6 小时</mdui-menu-item>
          <mdui-menu-item value="43200">每 12 小时</mdui-menu-item>
          <mdui-menu-item value="86400">每天</mdui-menu-item>
          <mdui-menu-item value="604800">每周</mdui-menu-item>
        </mdui-select>
        <p class="helper-text">相同内容的重复转发和仅排版变化不会提醒；停机恢复后合并为一次说明。</p>
      </template>
      <mdui-button type="submit" variant="filled" :disabled="busy">{{ busy ? '保存中……' : '保存计划' }}</mdui-button>
    </form>
    <ul v-if="plans.length" class="plan-list">
      <li v-for="plan in plans" :key="plan.id">
        <div>
          <strong>{{ plan.title }}</strong>
          <p>{{ plan.mode === 'main' ? '主助手' : '独立子对话' }} · {{ statusLabel(plan.status) }} · 下次 {{ minuteLocal(plan.next_run_at_epoch) }}</p>
          <p v-if="plan.watch_query" class="plan-watch">
            <span>关注「{{ plan.watch_query }}」</span>
            <span v-if="plan.watch_url">官网：{{ plan.watch_url }}</span>
            <span>{{ plan.watch_scope === 'all' ? '全部资料' : plan.watch_scope === 'public' ? '公共库' : '私人资料' }}</span>
            <span>上次检查 {{ dateTime(plan.last_watch_checked_epoch) }}</span>
            <span>上次成功 {{ dateTime(plan.last_success_epoch) }}</span>
            <span v-if="plan.run_count">已触发 {{ plan.run_count }} 次</span>
          </p>
          <p v-if="plan.pending_run_id" class="plan-delta">检测到变化，正在等待主助手处理完成；成功后才会记为已通知。</p>
          <p v-if="plan.last_watch_delta_json?.summary" class="plan-delta">最近变化：{{ plan.last_watch_delta_json.summary }}</p>
          <p v-if="plan.last_error_code" class="error-text">最近检查未完成：{{ plan.last_error_code === 'watch_too_broad' ? '关注范围过大，请缩小关键词' : plan.last_error_code === 'dispatch_failed' ? '唤醒派发失败，已暂停' : plan.last_error_code === 'watch_check_failed' ? '资料检查失败，稍后重试' : plan.last_error_code }}</p>
        </div>
        <div class="plan-actions">
          <mdui-button v-if="plan.status === 'active'" variant="text" :disabled="busy" @click="setStatus(plan, 'pause')">暂停</mdui-button>
          <mdui-button v-if="plan.status === 'paused'" variant="text" :disabled="busy" @click="setStatus(plan, 'resume')">恢复</mdui-button>
          <mdui-button v-if="plan.status === 'active' || plan.status === 'paused'" variant="text" :disabled="busy" @click="setStatus(plan, 'cancel')">取消计划</mdui-button>
        </div>
      </li>
    </ul>
    <p v-else class="helper-text">还没有定时任务或持续关注。</p>
  </section>
</template>
<style scoped>
.agent-task-plans { margin-top: 32px; border-top: 1px solid #e5dfee; padding-top: 20px; }
.plans-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.plans-heading > div > p { max-width: 680px; color: #687184; line-height: 1.65; }
.plans-form { display: grid; gap: 14px; max-width: 680px; }
.plans-form label { display: grid; gap: 6px; }
.plans-form label.checkbox-field { display: flex; align-items: center; gap: 8px; }
.plan-list { list-style: none; padding: 0; margin: 18px 0 0; }
.plan-list li { display: flex; justify-content: space-between; gap: 12px; padding: 14px 0; border-bottom: 1px solid #eee8f4; }
.plan-list li p { font-size: 13px; color: #777; margin: 6px 0 0; }
.plan-watch { display: flex; flex-wrap: wrap; gap: 6px 12px; }
.plan-watch span { padding: 2px 7px; border-radius: 999px; background: #f0f3fa; }
.plan-delta { color: #4d6484 !important; }
.plan-actions { display: flex; align-items: flex-start; gap: 4px; flex-wrap: wrap; }
@media (max-width: 640px) {
  .plan-list li { flex-direction: column; }
}
</style>
