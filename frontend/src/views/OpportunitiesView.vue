<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { adoptOpportunity, getStudentIntent, listOpportunities, saveStudentIntent, type Opportunity, type StudentIntent } from '../api/journeys';
import { readMduiChecked, readMduiValue } from '../utils/mdui';

const router = useRouter();
const profile = ref<StudentIntent | null>(null);
const interests = ref('');
const saved = ref('');
const items = ref<Opportunity[]>([]);
const scopeNote = ref('');
const scanned = ref(0);
const total = ref(0);
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const success = ref('');
let disposed = false;
let requestVersion = 0;
const dirty = computed(() => profile.value && JSON.stringify({ ...profile.value, interests: interests.value }) !== saved.value);

onMounted(() => void refresh());
onUnmounted(() => { disposed = true; requestVersion += 1; });

function applyProfile(value: StudentIntent): void {
  profile.value = value;
  interests.value = value.interests.join('、');
  saved.value = JSON.stringify({ ...value, interests: interests.value });
}
async function refresh(): Promise<void> {
  const request = ++requestVersion;
  loading.value = true;
  error.value = '';
  try {
    const [intent, result] = await Promise.all([getStudentIntent(), listOpportunities()]);
    if (disposed || request !== requestVersion) return;
    applyProfile(intent);
    applyResults(result);
  } catch (failure) { if (!disposed && request === requestVersion) error.value = message(failure); }
  finally { if (!disposed && request === requestVersion) loading.value = false; }
}
function applyResults(result: Awaited<ReturnType<typeof listOpportunities>>): void {
  items.value = result.items;
  scopeNote.value = result.scope_note;
  scanned.value = result.scanned;
  total.value = result.total_events;
}
async function save(): Promise<void> {
  if (!profile.value || busy.value) return;
  busy.value = true;
  error.value = '';
  success.value = '';
  try {
    const result = await saveStudentIntent({ ...profile.value, interests: interests.value.split(/[、,，\n]/).map(word => word.trim()).filter(Boolean) });
    if (disposed) return;
    applyProfile(result);
    success.value = '兴趣与空闲窗口已保存。';
    const matches = await listOpportunities();
    if (!disposed) applyResults(matches);
  } catch (failure) { if (!disposed) error.value = message(failure); }
  finally { if (!disposed) busy.value = false; }
}
async function adopt(item: Opportunity): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    const value = await adoptOpportunity(item);
    if (!disposed) await router.push({ path: '/matters/journeys', query: { id: value.id } });
  } catch (failure) { if (!disposed) error.value = message(failure); }
  finally { if (!disposed) busy.value = false; }
}
function setWeekday(day: number, event: Event): void {
  if (!profile.value) return;
  const values = new Set(profile.value.availability.weekdays);
  if (readMduiChecked(event)) values.add(day); else values.delete(day);
  profile.value.availability.weekdays = [...values].sort((left, right) => left - right);
}
function message(value: unknown): string { return value instanceof ApiError ? value.message : '机会暂时无法加载，请稍后重试。'; }
</script>

<template>
  <section class="page-section opportunities-page" aria-labelledby="opportunities-heading">
    <header class="opportunities-header">
      <div>
        <p class="eyebrow">兴趣、条件与参与成本</p>
        <h1 id="opportunities-heading">发现适合我的机会</h1>
        <p>从已入库通知里找值得关注的事，先看理由，再核对资格与时间。</p>
      </div>
      <div class="opportunity-actions">
        <mdui-button variant="text" @click="router.push('/matters')">返回事务</mdui-button>
        <mdui-button variant="text" @click="router.push('/matters/journeys')">办理路线</mdui-button>
        <mdui-button variant="outlined" :disabled="busy || loading" @click="refresh">刷新</mdui-button>
      </div>
    </header>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="success" class="success-text" role="status">{{ success }}</p>
    <p v-if="loading" class="helper-text" role="status">正在匹配机会……</p>
    <div v-else class="opportunities-layout">
      <form v-if="profile" class="profile-card" @submit.prevent="save">
        <h2>我的兴趣与空闲</h2>
        <p class="self-facts">{{ profile.availability.weekdays.length === 7 ? '每天可用' : `可用星期 ${profile.availability.weekdays.map(day => '一二三四五六日'[day - 1]).join('、')}` }} · {{ profile.availability.start_time }}–{{ profile.availability.end_time }}（{{ profile.availability.timezone }}）</p>
        <mdui-text-field label="兴趣关键词" :value="interests" placeholder="例如：人工智能、志愿服务、竞赛" @input="interests = readMduiValue($event)"></mdui-text-field>
        <mdui-text-field label="本人资料（供对照要求）" :value="profile.facts" rows="3" autosize max-rows="6" placeholder="例如：本科二年级，语言成绩待取得" @input="profile.facts = readMduiValue($event)"></mdui-text-field>
        <div class="opportunity-grid">
          <mdui-text-field label="每天从" type="time" :value="profile.availability.start_time" @input="profile.availability.start_time = readMduiValue($event)"></mdui-text-field>
          <mdui-text-field label="到" type="time" :value="profile.availability.end_time" @input="profile.availability.end_time = readMduiValue($event)"></mdui-text-field>
        </div>
        <fieldset>
          <legend>可用星期</legend>
          <mdui-checkbox v-for="(day, index) in ['一', '二', '三', '四', '五', '六', '日']" :key="day" :checked="profile.availability.weekdays.includes(index + 1)" @change="setWeekday(index + 1, $event)">{{ day }}</mdui-checkbox>
        </fieldset>
        <div class="opportunity-actions">
          <mdui-button type="submit" variant="filled" :disabled="busy || !dirty">保存并重新匹配</mdui-button>
          <span v-if="dirty" class="helper-text">偏好有未保存修改，右侧仍为已保存偏好的结果。</span>
        </div>
      </form>
      <section class="opportunity-results">
        <p class="helper-text">{{ scopeNote }} 已扫描 {{ scanned }} 条资料，共 {{ total }} 条事件。<template v-if="items.length">以下仅展示有匹配理由的条目。</template></p>
        <article v-for="item in items" :key="item.event_id" class="opportunity-card">
          <div class="opportunity-card-heading">
            <h2>{{ item.title }}</h2>
            <span class="opportunity-match">{{ item.matched_interests.length ? `匹配 ${item.matched_interests.join('、')}` : '相关机会' }}</span>
          </div>
          <p v-if="item.deadline?.local_date">截止：{{ item.deadline.local_date }}<template v-if="item.deadline.local_time"> {{ item.deadline.local_time.slice(0, 5) }}</template></p>
          <p v-if="item.actor || item.stage">办理主体：{{ item.actor || '待核实' }} · 阶段：{{ item.stage || '待核实' }}</p>
          <p v-if="item.scope">适用范围：{{ item.scope }}</p>
          <p class="opportunity-reason">{{ item.reason }}</p>
          <p v-if="item.materials.length"><strong>材料：</strong>{{ item.materials.join('、') }}</p>
          <p v-if="item.submission"><strong>提交方式：</strong>{{ item.submission }}</p>
          <p class="helper-text">请对照适用范围与本人资料核实报名资格。</p>
          <ul v-if="item.risks.length" class="opportunity-risks"><li v-for="risk in item.risks" :key="risk">{{ risk }}</li></ul>
          <ul v-if="item.conflicts.length" class="opportunity-risks"><li v-for="conflict in item.conflicts" :key="conflict">{{ conflict }}</li></ul>
          <div class="opportunity-actions">
            <mdui-button variant="filled" :disabled="busy || Boolean(dirty)" @click="adopt(item)">关注并建立办理路线</mdui-button>
            <mdui-button v-if="item.notice_id" variant="text" @click="router.push({ path: '/inbox', query: { notice: item.notice_id } })">核对来源通知</mdui-button>
          </div>
          <small v-if="dirty">保存兴趣和空闲窗口后才能关注机会。</small>
        </article>
        <div v-if="!items.length" class="opportunity-empty">
          <h2>没有匹配的机会</h2>
          <p>填写兴趣关键词，或扩大可用时间窗口后再试。</p>
        </div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.opportunities-page{max-width:1180px}
.opportunities-header{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;flex-wrap:wrap}
.opportunities-header h1{margin:3px 0 6px;font-size:clamp(1.8rem,4vw,2.5rem)}
.opportunities-header p:last-child{margin:0;color:var(--app-on-surface-variant,#667080);font-size:12px}
.opportunity-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.opportunities-layout{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,1.5fr);gap:18px;align-items:start}
.profile-card,.opportunity-card,.opportunity-empty{padding:18px;border:1px solid #dfe3ec;border-radius:16px;background:#fff;min-width:0;overflow-wrap:anywhere}
.profile-card{display:grid;gap:12px;position:sticky;top:76px}
.profile-card h2,.opportunity-card h2,.opportunity-empty h2{margin:0;font-size:1.05rem}
.self-facts{margin:0;color:#667080;font-size:11px;line-height:1.5}
.profile-card fieldset{display:flex;gap:10px;flex-wrap:wrap;padding:8px 0;border:0}
.profile-card fieldset legend{width:100%;color:#596275;font-size:11px;margin-bottom:4px}
.opportunity-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.opportunity-results{display:grid;gap:12px;min-width:0}
.opportunity-card{display:grid;gap:7px}
.opportunity-card-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.opportunity-match{padding:2px 7px;border-radius:999px;background:#eef3fc;color:#315da8;font-size:10px;white-space:nowrap}
.opportunity-card p{margin:0;font-size:12px;line-height:1.65;color:#4f5968}
.opportunity-reason{color:#42664c!important}
.opportunity-risks{margin:0;padding:10px 10px 10px 28px;border-radius:9px;background:#fff6e4;color:#70521e;font-size:11px;line-height:1.6}
.opportunity-card small{display:block;color:#8b5c18;font-size:10px}
.opportunity-empty{text-align:center;color:#667080}
@media(max-width:800px){.opportunities-layout{grid-template-columns:1fr}.profile-card{position:static;padding:14px}.opportunity-card{padding:14px}.opportunities-header{align-items:stretch;flex-direction:column;gap:8px}.opportunity-actions{justify-content:flex-start}.opportunity-grid{grid-template-columns:1fr}}
</style>
