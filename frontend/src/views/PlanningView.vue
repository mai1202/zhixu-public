<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute, RouterLink } from 'vue-router';
import { apiRequest } from '../api/client';
import { applyPlanning, getPlanningPreferences, getPlanningRun, previewPlanning, savePlanningPreferences, type PlanningPreferences, type PlanningResult } from '../api/innovation';

const route = useRoute();
const preferences = ref<PlanningPreferences | null>(null);
const routes = ref<Array<{ id: string; title: string }>>([]);
const entries = ref<Array<{ id: string; title: string; version: number; local_date: string; start_at: string | null; planning_policy?: { movable?: boolean; locked?: boolean; earliest_date?: string; latest_date?: string } }>>([]);
const selected = ref<string[]>([]);
const selectedEntries = ref<string[]>([]);
const run = ref<PlanningResult | null>(null);
const error = ref('');
const message = ref('');
const busy = ref(false);
const ranges = ref<Record<string, { first: string; last: string }>>({});
// 用业务语言解释试算结果，避免把策略名称当成用户需要理解的算法参数。
const strategyNames: Record<string, string> = { deadline: '截止优先', slack: '单步骤余量优先', priority: '事务优先级', dependency_pressure: '前置依赖与空闲余量' };
function affectedSteps(ids: string[]): string {
  // 把诊断中的内部标识转为步骤名称，方便核对哪些工作共同占满了时间。
  return run.value?.result.diagnostics?.units.filter(item => ids.includes(item.id)).map(item => item.title).join('、') ?? '';
}

async function perform(action: () => Promise<void>): Promise<void> {
  if (busy.value) return; busy.value = true; error.value = ''; message.value = '';
  try { await action(); } catch (failure) { error.value = failure instanceof Error ? failure.message : '操作未完成。'; }
  finally { busy.value = false; }
}
async function load(): Promise<void> {
  preferences.value = await getPlanningPreferences();
  const result = await apiRequest<{ items: Array<{ id: string; title: string; is_owner: boolean }> }>('/api/v1/journeys');
  routes.value = result.items.filter(item => item.is_owner);
  // 日程 API 限定日期范围，避免无界加载个人安排。
  const first = new Date(); const last = new Date(first.getTime() + 14 * 86400000);
  const day = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const schedule = await apiRequest<{ items: Array<typeof entries.value[number] & { item_type: string }> }>(`/api/v1/schedule?start_date=${day(first)}&end_date=${day(last)}`);
  // 跨日安排只显示一个许可入口，版本与范围直接采用后端返回值。
  entries.value = [...new Map(schedule.items.filter(item => item.item_type === 'personal' && item.start_at).map(item => [item.id, item])).values()];
  for (const entry of entries.value) ranges.value[entry.id] = { first: entry.planning_policy?.earliest_date ?? entry.local_date, last: entry.planning_policy?.latest_date ?? entry.local_date };
}
async function save(): Promise<void> {
  if (!preferences.value) return;
  preferences.value = await savePlanningPreferences({ expected_version: preferences.value.version, automatic: preferences.value.automatic, availability: preferences.value.availability, journey_ids: preferences.value.journey_ids ?? [] });
  message.value = '规划设置已保存。';
}
async function policy(entry: typeof entries.value[number], movable: boolean, locked = false): Promise<void> {
  const range = ranges.value[entry.id]!;
  const result = await apiRequest<{ version: number; policy: NonNullable<typeof entry.planning_policy> }>(`/api/v1/schedule-entries/${entry.id}/planning-policy`, { method: 'PATCH', body: JSON.stringify({ expected_version: entry.version, movable, locked, earliest_date: range.first, latest_date: range.last }) });
  entry.version = result.version; entry.planning_policy = result.policy;
  message.value = '此安排的许可已保存；两小时保护窗口始终有效。';
}
async function preview(): Promise<void> {
  run.value = await previewPlanning({ journey_ids: selected.value, entry_ids: selectedEntries.value, availability: preferences.value?.availability });
}
async function apply(): Promise<void> { if (run.value) { run.value = await applyPlanning(run.value); message.value = '已按数据库校验结果应用方案。'; } }
onMounted(() => void perform(async () => { await load(); if (typeof route.query.run === 'string') run.value = await getPlanningRun(route.query.run); }));
</script>

<template>
  <main class="page-section planning-page">
    <h1>统一安排事务</h1><p>未来14天，最多10条路线、100个未完成步骤，共用同一份可用时间。</p>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p><p v-if="message" role="status">{{ message }}</p>
    <section v-if="preferences"><h2>可用时间与自动调整</h2>
      <label>时区 <input v-model="preferences.availability.timezone" /></label>
      <label>开始 <input v-model="preferences.availability.start_time" type="time" /></label><label>结束 <input v-model="preferences.availability.end_time" type="time" /></label>
      <label v-for="day in [1,2,3,4,5,6,7]" :key="day"><input v-model="preferences.availability.weekdays" type="checkbox" :value="day" />周{{ day }}</label>
      <label><input v-model="preferences.automatic" type="checkbox" />允许对已纳入的路线及明确许可的安排自动局部调整</label>
      <p>课程、锁定、已经开始或未来两小时内开始的安排受保护。</p>
      <p v-if="!preferences.automatic_available">自动调整暂不可用，当前可以试算并手动应用。</p>
      <mdui-button :disabled="busy" @click="perform(save)">保存规划设置</mdui-button>
    </section>
    <section><h2>选择办理路线</h2><p v-if="!routes.length">先为事务建立办理路线并填写耗时。<RouterLink to="/matters/journeys">前往办理路线</RouterLink></p>
      <div v-for="item in routes" :key="item.id"><label><input v-model="selected" type="checkbox" :value="item.id" />{{ item.title }}</label><label v-if="preferences"><input v-model="preferences.journey_ids" type="checkbox" :value="item.id" />纳入自动调整（保存设置后生效）</label></div>
    </section>
    <section><h2>个人安排的移动许可</h2><p>默认不移动。开启时请明确允许日期范围，原时长保持不变。</p>
      <article v-for="entry in entries" :key="entry.id"><strong>{{ entry.title }}</strong><p>{{ entry.start_at }}</p>
        <label>最早日期 <input v-model="ranges[entry.id]!.first" type="date" /></label><label>最晚日期 <input v-model="ranges[entry.id]!.last" type="date" /></label>
        <mdui-button :disabled="busy" variant="outlined" @click="perform(() => policy(entry, !entry.planning_policy?.movable))">{{ entry.planning_policy?.movable ? '关闭移动许可' : '明确允许移动' }}</mdui-button>
        <mdui-button :disabled="busy" variant="text" @click="perform(() => policy(entry, Boolean(entry.planning_policy?.movable), !entry.planning_policy?.locked))">{{ entry.planning_policy?.locked ? '解除锁定' : '锁定此安排' }}</mdui-button>
        <label><input v-model="selectedEntries" type="checkbox" :value="entry.id" />纳入本次试算</label>
      </article>
    </section>
    <mdui-button :disabled="busy" variant="filled" @click="perform(preview)">统一试算</mdui-button>
    <section v-if="run" aria-live="polite"><h2>方案与影响</h2><p>{{ run.result.status === 'fits' ? '当前输入下可以完整安排' : '仍有条件需要核对，原安排保持不变' }} · 移动{{ run.result.moved_count }}项 · 累计{{ run.result.moved_minutes }}分钟</p>
      <p v-for="note in run.result.assumptions" :key="note">{{ note }}</p>
      <p>本次方案依据：{{ strategyNames[run.result.strategy] ?? run.result.strategy }}</p>
      <template v-if="run.result.diagnostics">
        <h3>时间不足的依据</h3>
        <p v-if="!run.result.diagnostics.certificates.length">未发现能够直接证明容量不足的条件；仍以完整试算结果为准。</p>
        <ul><li v-for="(item, index) in run.result.diagnostics.certificates" :key="index">
          <p>{{ item.reason }}</p>
          <p v-if="item.start_at && item.end_at">时间范围：{{ new Date(item.start_at).toLocaleString() }} — {{ new Date(item.end_at).toLocaleString() }}</p>
          <p v-if="affectedSteps(item.unit_ids)">涉及步骤：{{ affectedSteps(item.unit_ids) }}</p>
        </li></ul>
        <p>{{ run.result.diagnostics.note }}</p>
        <details v-if="run.result.diagnostics.units.length"><summary>查看前置步骤与后续工作量</summary>
          <ul><li v-for="item in run.result.diagnostics.units" :key="item.id">{{ item.title }}：最长依赖链工作量为{{ item.critical_path_minutes }}分钟，最晚开始上界为{{ new Date(item.latest_start_bound).toLocaleString() }}。此上界未保证期间有足够空闲。</li></ul>
        </details>
      </template>
      <details v-if="run.result.comparison?.length"><summary>查看不同安排方式的比较</summary>
        <ul><li v-for="item in run.result.comparison" :key="item.strategy">{{ strategyNames[item.strategy] ?? item.strategy }}：{{ item.unresolved_count }}项未安排，移动{{ item.moved_count }}项、{{ item.moved_minutes }}分钟。</li></ul>
      </details>
      <ol><li v-for="(slot, index) in run.result.slots" :key="index">{{ slot.title }}：{{ new Date(slot.start_at).toLocaleString() }} — {{ new Date(slot.end_at).toLocaleString() }}</li></ol>
      <ul><li v-for="item in run.result.unresolved" :key="item.id">{{ item.reason }}</li></ul>
      <mdui-button :disabled="busy || run.result.status !== 'fits' || run.status === 'applied'" @click="perform(apply)">{{ run.status === 'applied' ? '已应用' : '确认应用完整方案' }}</mdui-button>
      <mdui-button v-if="run.change_set_id" :disabled="busy" variant="text" @click="perform(async () => { await apiRequest(`/api/v1/changes/${run!.change_set_id}/undo`, { method: 'POST', body: '{}' }); message = '已撤销本次调整。'; })">撤销本次调整</mdui-button>
    </section>
  </main>
</template>

<style scoped>
/* 多选、日期和长标题在手机换行，避免固定列导致横向溢出。 */
.planning-page section { margin: 22px 0; padding: 16px; border: 1px solid var(--app-outline, #ddd); border-radius: 14px; overflow-wrap: anywhere; }
.planning-page { max-width: 1100px; margin-inline: auto; }
.planning-page h2 { font-size: 1.2rem; margin: 0 0 14px; }
.planning-page input:not([type="checkbox"]) { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--app-outline, #bbb); background: var(--app-surface, #fff); color: inherit; font: inherit; min-width: 0; }
.planning-page input[type="checkbox"] { width: 17px; height: 17px; accent-color: #345fa6; }
label { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 6px 12px 6px 0; } input { max-width: 100%; box-sizing: border-box; }
article { margin: 12px 0; padding: 12px; border-top: 1px solid var(--app-outline, #ddd); } li { line-height: 1.8; }
</style>
