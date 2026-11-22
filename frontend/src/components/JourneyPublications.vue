<script setup lang="ts">
/** 分享前展示字段；持续信任必须由接收成员自己开启。 */
import { onMounted, ref } from 'vue';
import { apiRequest, ApiError } from '../api/client';
interface SharedEvent { title: string; actor: string | null; stage: string | null; scope: string | null; structured_deadline: { raw: string } | null; materials: string[]; submission: string | null }
interface State { trusted: boolean; preview: SharedEvent | null; preview_hash: string | null; items: { id: string; event: SharedEvent }[] }
const props = defineProps<{ journeyId: string; version: number; isOwner: boolean }>();
const emit = defineEmits<{ changed: [] }>();
const state = ref<State | null>(null);
const confirmed = ref(false);
const busy = ref(false);
const info = ref('');
const error = ref('');
const base = () => `/api/v1/journeys/${props.journeyId}`;
async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true; error.value = ''; info.value = '';
  try { await action(); } catch (failure) { error.value = failure instanceof ApiError ? failure.message : '暂时无法完成，请重试。'; }
  finally { busy.value = false; }
}
onMounted(() => void run(async () => { state.value = await apiRequest<State>(base() + '/publications'); }));
function trust(enabled: boolean): void { void run(async () => { state.value = await apiRequest<State>(base() + '/source-trust', { method: 'PUT', body: JSON.stringify({ enabled }) }); }); }
function publish(): void { if (!confirmed.value) return; void run(async () => {
  state.value = await apiRequest<State>(base() + '/publications', { method: 'POST', body: JSON.stringify({ expected_version: props.version, share_details: true, expected_snapshot_hash: state.value?.preview_hash }) });
  confirmed.value = false; emit('changed');
}); }
function adopt(id: string): void { void run(async () => {
  await apiRequest(`${base()}/publications/${id}/adopt`, { method: 'POST', body: '{}' });
  info.value = '已接收到个人通知，沿用已分享的字段，没有重新调用模型。';
}); }
</script>
<template>
  <section class="publications"><h2>共享通知要求</h2><p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="info" role="status">{{ info }}</p>
    <template v-if="state">
      <template v-if="isOwner && state.preview"><p>分享以下结构化字段，成员可以直接接收。私人通知原文、对话和课表不会加入分享。</p>
        <dl><dt>标题</dt><dd>{{ state.preview.title }}</dd><dt>对象与阶段</dt><dd>{{ [state.preview.actor, state.preview.stage, state.preview.scope].filter(Boolean).join(' · ') || '未填写' }}</dd>
          <dt>截止要求</dt><dd>{{ state.preview.structured_deadline?.raw || '未明确' }}</dd><dt>材料</dt><dd>{{ state.preview.materials.join('、') || '未填写' }}</dd><dt>提交方式</dt><dd>{{ state.preview.submission || '未填写' }}</dd></dl>
        <label><input v-model="confirmed" type="checkbox" />我已核对以上字段，同意分享给本路线成员</label>
        <mdui-button :disabled="busy || !confirmed" @click="publish">发布当前要求</mdui-button>
      </template>
      <template v-if="!isOwner"><p>开启信任后，发起人以后分享的要求会直接进入个人通知，不再重复解析。你仍可核对、修改自己的事务。</p>
        <mdui-button :disabled="busy" @click="trust(!state.trusted)">{{ state.trusted ? '撤回对发起人的信任' : '信任发起人后续分享' }}</mdui-button></template>
      <ul><li v-for="item in state.items" :key="item.id"><strong>{{ item.event.title }}</strong><p>{{ item.event.structured_deadline?.raw || '截止时间未明确' }} · {{ item.event.materials.join('、') }}</p>
        <mdui-button variant="outlined" :disabled="busy" @click="adopt(item.id)">接收到个人通知</mdui-button></li></ul>
    </template>
  </section>
</template>
<style scoped>
.publications{padding:22px;border:1px solid #ddd;border-radius:18px;overflow-wrap:anywhere}h2{font-size:1.2rem}dt{font-weight:600}dd{margin:4px 0 12px}label{display:block;margin:14px 0}li{margin:16px 0}
</style>
