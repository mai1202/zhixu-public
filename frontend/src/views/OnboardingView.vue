<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { getOnboarding, startOnboarding, updateOnboarding, type OnboardingState } from '../api/onboarding';
import { sourceSubmission } from '../api/submission';
import { useAuthStore } from '../stores/auth';
import { readMduiValue } from '../utils/mdui';

const router = useRouter();
const auth = useAuthStore();
const state = ref<OnboardingState | null>(null);
const goals = ref('');
const loading = ref(false);
const busy = ref(false);
const error = ref('');
const notice = ref('');
const reminderDelay = computed(() => {
  const seconds = state.value?.reminder_delay_seconds ?? 86400;
  return seconds < 3600 ? `${Math.ceil(seconds / 60)} 分钟` : `${Math.round(seconds / 360) / 10} 小时`;
});
let disposed = false;
let requestVersion = 0;

function fail(failure: unknown): void { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '引导暂时无法加载，请重试。'; }
async function refresh(): Promise<void> {
  const version = ++requestVersion;
  loading.value = true;
  try { const result = await getOnboarding(); if (!disposed && version === requestVersion) state.value = result; }
  catch (failure) { if (version === requestVersion) fail(failure); }
  finally { if (!disposed && version === requestVersion) loading.value = false; }
}
function onVisible(): void { if (document.visibilityState === 'visible' && !busy.value) void refresh(); }
onMounted(() => { void refresh(); document.addEventListener('visibilitychange', onVisible); });
onUnmounted(() => { disposed = true; requestVersion += 1; document.removeEventListener('visibilitychange', onVisible); });

async function save(patch: Parameters<typeof updateOnboarding>[1]): Promise<void> {
  if (!state.value || busy.value || loading.value) return;
  busy.value = true; error.value = ''; notice.value = '';
  try {
    const result = await updateOnboarding(state.value, patch);
    if (disposed) return;
    state.value = result;
    notice.value = patch.action === 'complete' ? '首次设置已完成，可以随时从设置目录重新查看。' : patch.action === 'snooze' ? '已推迟七天，本次进度会保留。' : '引导偏好已保存。';
  } catch (failure) { fail(failure); }
  finally { if (!disposed) busy.value = false; }
}
async function skip(key: string, event: Event): Promise<void> {
  if (!state.value) return;
  const control = event.currentTarget as HTMLElement & { checked: boolean };
  const checked = Boolean(control.checked);
  const skipped = new Set(state.value.skipped_sections);
  if (checked) skipped.add(key); else skipped.delete(key);
  await save({ skipped_sections: [...skipped] });
  // 自定义复选框先改变自身属性；保存失败时显式还原，避免页面显示与服务端意愿相反。
  if (!disposed && state.value) control.checked = state.value.skipped_sections.includes(key);
}
async function reminderChanged(event: Event): Promise<void> {
  const control = event.currentTarget as HTMLElement & { checked: boolean };
  await save({ reminder_enabled: Boolean(control.checked) });
  if (!disposed && state.value) control.checked = state.value.reminder_enabled;
}
async function begin(): Promise<void> {
  if (!state.value?.model_ready || busy.value || loading.value) return;
  busy.value = true; error.value = '';
  const text = goals.value.trim();
  try {
    // 复用既有提交回执：响应丢失或刷新后重试相同目标仍对应同一次运行，不存储目标正文。
    const submission = await sourceSubmission(`onboarding:${auth.user?.id ?? ''}`, { goals: text });
    if (disposed) return;
    const result = await startOnboarding(text, submission.key);
    submission.complete();
    if (!disposed) await router.push({ path: '/agent', query: { conversation: result.conversation_id } });
  } catch (failure) { fail(failure); }
  finally { if (!disposed) busy.value = false; }
}
</script>

<template>
  <section class="page-section onboarding-page" aria-labelledby="onboarding-heading">
    <RouterLink to="/settings">← 设置目录</RouterLink>
    <header><div><p class="eyebrow">首次使用 / 引导 Agent</p><h1 id="onboarding-heading">一起把知序配好</h1>
      <p>告诉助手你要管理什么，它会读取已有配置、帮助添加真实事务，并逐项引导。</p></div>
      <mdui-button variant="outlined" :disabled="loading || busy" @click="refresh">重新检查进度</mdui-button></header>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="notice" class="success-text" role="status">{{ notice }}</p>
    <p v-if="loading && !state" role="status">正在核对已有设置……</p>
    <template v-if="state">
      <section class="onboarding-agent-card">
        <h2>让引导 Agent 开始</h2>
        <p>当前通知自动维护：{{ state.automatic_notifications ? '已开启' : '已关闭' }}。新确认设备默认接管已发现群，可排除或改为逐项选择；附件采集与转化可独立关闭。</p>
        <mdui-text-field label="想先管理哪些事情？" :value="goals" maxlength="4000" rows="3" autosize max-rows="8" placeholder="例如：帮我管理课程作业和大挑材料；我通常晚上做事，提醒发到我的邮箱。" @input="goals = readMduiValue($event)"></mdui-text-field>
        <mdui-button variant="filled" :disabled="busy || loading || !state.model_ready" :loading="busy" @click="begin">开始或继续 Agent 引导</mdui-button>
        <p v-if="!state.model_ready" class="helper-text">主助手还没有可用规划模型。<RouterLink class="app-link" to="/settings/models">先连接模型</RouterLink>，再到<RouterLink class="app-link" to="/settings/assistant">助手推理</RouterLink>选择主助手模型。</p>
        <p class="helper-text">点击后会提交一次真实主助手请求。缺失日期或个人偏好时，助手会询问，不会代编。</p>
      </section>
      <h2>配置进度 · {{ state.completed_count }}/{{ state.step_count }}</h2>
      <ol class="onboarding-steps">
        <li v-for="step in state.steps" :key="step.key" :data-status="step.status">
          <div><strong>{{ step.title }}</strong><span>{{ step.status === 'done' ? '已配置' : step.status === 'skipped' ? '暂不需要' : '待配置' }}</span><p>{{ step.detail }}</p></div>
          <div class="onboarding-step-actions"><RouterLink class="app-link" :to="step.href">{{ step.status === 'done' ? '检查设置' : '去配置' }}</RouterLink>
            <mdui-checkbox v-if="step.status !== 'done'" :checked="state.skipped_sections.includes(step.key)" :disabled="busy || loading" @change="skip(step.key, $event)">暂不需要</mdui-checkbox></div>
        </li>
      </ol>
      <section class="onboarding-reminder"><h2>未完成设置时提醒我</h2>
        <mdui-checkbox :checked="state.reminder_enabled" :disabled="busy || loading" @change="reminderChanged">开始引导约 {{ reminderDelay }}后仍未完成，给已验证邮箱发一次提醒</mdui-checkbox>
        <p>此开关即时保存。尊重安静时段；“稍后设置”推迟七天，关闭或完成后未发送的提醒会取消。邮件服务未配置时不会声称已发送。</p>
        <p v-if="state.reminder_queued">本轮设置提醒已排入邮件队列，实际结果可在邮件页查看。</p>
      </section>
      <footer><mdui-button variant="filled" :disabled="busy || !state.ready_to_complete" @click="save({ action: 'complete' })">完成首次设置</mdui-button>
        <mdui-button variant="text" :disabled="busy" @click="save({ action: 'snooze' })">七天后再设置</mdui-button><RouterLink to="/today">回到今天</RouterLink></footer>
    </template>
  </section>
</template>

<style scoped>
/* 配置入口、真实进度和提醒偏好分层展示，移动端操作保持自然换行。 */
.onboarding-page { max-width: 1050px; }
header, footer, .onboarding-steps li { display: flex; justify-content: space-between; align-items: center; gap: 18px; flex-wrap: wrap; }
header { margin: 18px 0; }
p { color: var(--app-on-surface-variant); line-height: 1.75; }
.onboarding-agent-card, .onboarding-reminder { padding: 22px; border: 1px solid var(--app-outline, #ddd); border-radius: 18px; margin: 20px 0; }
label { display: grid; gap: 10px; margin: 18px 0; }
textarea { width: 100%; box-sizing: border-box; resize: vertical; }
.onboarding-steps { list-style: none; padding: 0; }
.onboarding-steps li { padding: 18px 0; border-bottom: 1px solid var(--app-outline, #ddd); }
.onboarding-steps span { font-size: 12px; margin-left: 14px; color: var(--app-on-surface-variant); }
.onboarding-steps li[data-status='done'] span { color: #187359; }
.onboarding-step-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
footer { justify-content: flex-start; padding: 12px 0; }
</style>
