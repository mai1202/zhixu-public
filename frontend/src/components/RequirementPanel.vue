<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { apiRequest } from '../api/client';
import { getRequirements, type RequirementState } from '../api/innovation';

const props = defineProps<{ eventId: string }>();
const state = ref<RequirementState | null>(null);
const error = ref('');
let generation = 0;
watch(() => props.eventId, async (id) => {
  const version = ++generation; state.value = null; error.value = '';
  try { const result = await getRequirements(id); if (version === generation) state.value = result; }
  catch (failure) { if (version === generation) error.value = failure instanceof Error ? failure.message : '要求暂时无法读取。'; }
}, { immediate: true });
onBeforeUnmount(() => { generation += 1; });
const names: Record<string, string> = { deadline: '截止', time_range: '活动时段', material: '材料', materials: '材料', submission: '提交方式', location: '地点', cancelled: '取消状态' };
const statuses: Record<string, string> = { active: '当前采用', superseded: '已被替代', retracted: '已撤回', undone: '已撤销', legacy: '历史记录' };
function display(value: unknown): string {
  if (value == null) return '未知';
  if (typeof value === 'boolean') return value ? '已取消' : '未取消';
  if (Array.isArray(value)) return value.join('、') || '暂无明确要求';
  if (typeof value === 'object') { const data = value as Record<string, unknown>; return String(data.raw ?? JSON.stringify(value)); }
  return String(value);
}
async function toggle(field: string): Promise<void> {
  if (!state.value) return;
  try {
    state.value = await apiRequest<RequirementState>(`/api/v1/events/${props.eventId}/field-policy`, { method: 'PATCH', body: JSON.stringify({ expected_version: state.value.version, field, follow_source: state.value.field_policies[field] === false }) });
  } catch (failure) { error.value = failure instanceof Error ? failure.message : '设置未保存。'; }
}
</script>

<template>
  <section class="requirement-panel" aria-label="当前要求与依据">
    <h2>当前要求与依据</h2>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="state">
      <dl><template v-for="(value, key) in state.current" :key="key"><dt>{{ names[key] ?? key }}</dt><dd>{{ display(value) }}<mdui-button variant="text" @click="toggle(String(key))">{{ state.field_policies[key] === false ? '已保留个人覆盖，改为跟随来源' : '跟随来源，改为保留个人覆盖' }}</mdui-button></dd></template></dl>
      <details v-for="claim in state.claims" :key="claim.id"><summary>{{ names[claim.field.split(':')[0] ?? ''] ?? claim.field }} · {{ display(claim.value) }} · {{ statuses[claim.status] ?? '历史记录' }}</summary>
        <p v-if="claim.basis_kind === 'user_confirmation'">此期限经用户核对采用。</p>
        <p v-if="claim.evidence_check">日期局部校验：{{ claim.evidence_check.reason }} 该检查不能代替原文语义核对。</p>
        <blockquote v-for="(quote, index) in claim.evidence" :key="index">{{ quote.quote }}</blockquote><RouterLink :to="`/inbox/${claim.source_id}`">查看来源</RouterLink>
      </details>
    </template>
  </section>
</template>

<style scoped>
/* 字段证据保持可读，不在小屏使用固定宽度。 */
.requirement-panel { padding: 18px; margin: 20px 0; border: 1px solid var(--app-outline, #ddd); border-radius: 14px; overflow-wrap: anywhere; }
dt { font-weight: 500; } dd { margin: 4px 0 14px; line-height: 1.7; } blockquote { white-space: pre-wrap; }
</style>
