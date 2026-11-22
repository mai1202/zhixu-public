<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { getOnboarding, updateOnboarding, type OnboardingState } from '../api/onboarding';
import { ApiError } from '../api/client';
import AppIcon from './AppIcon.vue';
const state = ref<OnboardingState | null>(null);
const error = ref('');
const busy = ref(false);
let disposed = false;
onMounted(async () => {
  try { const result = await getOnboarding(); if (!disposed) state.value = result; }
  catch { /* 首页主数据不依赖引导；设置目录仍保留完整引导入口。 */ }
});
onUnmounted(() => { disposed = true; });
async function snooze(): Promise<void> {
  if (!state.value || busy.value) return;
  busy.value = true;
  try { const result = await updateOnboarding(state.value, { action: 'snooze' }); if (!disposed) state.value = result; }
  catch (failure) { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '稍后设置没有保存，请重试。'; }
  finally { if (!disposed) busy.value = false; }
}
</script>

<template>
  <section v-if="state?.show_prompt" class="onboarding-prompt" aria-labelledby="onboarding-prompt-heading">
    <div><span>首次使用 · 已准备 {{ state.completed_count }}/{{ state.step_count }}</span><h2 id="onboarding-prompt-heading">让引导 Agent 帮你配好知序</h2>
      <p>从接管群、附件与通知转化，到第一批事务、课表和邮件提醒，按实际配置逐项继续。</p>
      <p v-if="error" role="alert" class="error-text">{{ error }}</p></div>
    <div class="onboarding-prompt-actions"><RouterLink class="app-button-link" to="/onboarding">{{ state.completed_count ? '继续配置' : '开始首次设置' }}<AppIcon name="arrow_forward" /></RouterLink>
      <mdui-button variant="text" :disabled="busy" @click="snooze">稍后设置</mdui-button></div>
  </section>
</template>

<style scoped>
/* 引导入口独立于设备安装提示，用户可看到完整工作流，也能暂时收起。 */
.onboarding-prompt { display: flex; flex-wrap: wrap; gap: 18px; justify-content: space-between; align-items: center; padding: 20px; border: 1px solid var(--app-outline, #ddd); border-radius: 18px; margin: 18px 0; background: var(--app-surface-container, #f4f6ff); }
h2 { margin: 8px 0; font-size: 20px; }
p, span { color: var(--app-on-surface-variant); font-size: 13px; line-height: 1.65; margin: 6px 0; }
.onboarding-prompt-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
</style>
