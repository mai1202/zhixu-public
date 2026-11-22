<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, toRaw, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { listTasks, type TaskSummary } from '../api/business';
import { useAuthStore } from '../stores/auth';
import JourneyCollaboration from '../components/JourneyCollaboration.vue';
import JourneyPublications from '../components/JourneyPublications.vue';
import { readMduiChecked, readMduiNumber, readMduiValue, readMduiValues } from '../utils/mdui';
import { actOnStep, closeJourneyInvite, createJourney, getJourney, getStudentIntent, inviteJourney, joinJourney,
  leaveJourney, listJourneys, previewJourneyPlan, refreshJourneySource, revokeJourneyMember, saveJourney, scheduleJourney,
  type Availability, type Journey, type JourneyPlan, type JourneyStep } from '../api/journeys';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const journeys = ref<Journey[]>([]);
const total = ref(0);
const tasks = ref<TaskSummary[]>([]);
const selected = ref<Journey | null>(null);
const draft = ref<JourneyStep[]>([]);
const taskId = ref('');
const loading = ref(true);
const busy = ref(false);
const error = ref('');
const success = ref('');
const editing = ref(false);
const plan = ref<JourneyPlan | null>(null);
const joinCode = ref('');
const alias = ref('');
const sharedTitle = ref('');
const shareConfirmed = ref(false);
const inviteCode = ref('');
const inviteCopied = ref(false);
const availability = ref<Availability>({ timezone: 'Asia/Shanghai', start_time: '18:00', end_time: '22:00', weekdays: [1, 2, 3, 4, 5, 6, 7] });
let disposed = false;
let requestVersion = 0;

const progress = computed(() => selected.value ? `${selected.value.steps.filter(step => step.status === 'done').length}/${selected.value.steps.length}` : '');
const nextStep = computed(() => selected.value?.steps.find(step => step.status !== 'done'
  && step.assigned_user_id === auth.user?.id
  && step.depends_on.every(id => selected.value?.steps.some(parent => parent.id === id && parent.status === 'done'))));
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(selected.value?.steps ?? []));
const canCommit = computed(() => plan.value && !plan.value.unresolved.length && plan.value.slots.length > 0
  && ['fits', 'beyond_horizon'].includes(plan.value.status));
const planLabel = computed(() => ({ fits: '当前窗口可以安排', insufficient: '截止前的可用时间不足',
  needs_estimate_or_handoff: '需要补齐耗时或等待交接', beyond_horizon: '超出未来两周的试算范围',
  deadline_unknown: '请先在事务详情确认截止日期', overdue: '截止已过，请先核对期限',
  source_changed: '来源要求变化，请先同步路线', task_closed: '事务已关闭' } as Record<string, string>)[plan.value?.status ?? '']);

onMounted(() => {
  // 邀请令牌放在片段中，避免进入服务器访问日志；加入仍需本人确认昵称。
  const params = new URLSearchParams(route.hash.slice(1));
  const invitation = params.get('invite');
  if (invitation) {
    joinCode.value = invitation;
    void router.replace({ path: route.path, query: route.query, hash: '' });
  }
  void refresh();
});
onUnmounted(() => { disposed = true; requestVersion += 1; inviteCode.value = ''; });
watch(() => route.query.id, () => { if (!loading.value) void refresh(); });
watch(availability, () => { plan.value = null; }, { deep: true });

function selectJourney(value: Journey): void {
  selected.value = value;
  draft.value = structuredClone(toRaw(value.steps));
  editing.value = false;
  plan.value = null;
  sharedTitle.value = value.shared_title ?? '';
  shareConfirmed.value = false;
  inviteCode.value = '';
}

async function refresh(): Promise<void> {
  const request = ++requestVersion;
  loading.value = true;
  error.value = '';
  try {
    const [routes, taskResult, intent] = await Promise.all([listJourneys(), listTasks('open', 100), getStudentIntent()]);
    const id = typeof route.query.id === 'string' ? route.query.id : selected.value?.id;
    const current = id ? routes.items.find(item => item.id === id) ?? await getJourney(id) : routes.items[0];
    if (disposed || request !== requestVersion) return;
    journeys.value = routes.items;
    total.value = routes.total;
    tasks.value = taskResult.items;
    if (typeof route.query.task === 'string') {
      taskId.value = route.query.task;
      if (!tasks.value.some(item => item.id === taskId.value)) {
        const { getTaskDetail } = await import('../api/ops');
        const detail = await getTaskDetail(taskId.value);
        if (disposed || request !== requestVersion) return;
        tasks.value.push(detail.task);
      }
    }
    availability.value = structuredClone(intent.availability);
    if (current) selectJourney(current);
    else { selected.value = null; draft.value = []; }
  } catch (failure) {
    if (!disposed && request === requestVersion) error.value = message(failure);
  } finally {
    if (!disposed && request === requestVersion) loading.value = false;
  }
}

async function perform(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  success.value = '';
  try { await action(); }
  catch (failure) {
    if (!disposed) {
      error.value = message(failure);
      plan.value = null;
    }
  } finally { if (!disposed) busy.value = false; }
}

function apply(value: Journey): void {
  if (disposed) return;
  const index = journeys.value.findIndex(item => item.id === value.id);
  if (index >= 0) journeys.value[index] = value;
  else { journeys.value.unshift(value); total.value += 1; }
  selectJourney(value);
}

function create(): void {
  if (!taskId.value) return;
  void perform(async () => {
    const value = await createJourney(taskId.value);
    apply(value);
    if (!disposed) await router.replace({ path: '/matters/journeys', query: { id: value.id } });
  });
}
function save(): void {
  const current = selected.value;
  if (!current || !draft.value.length) return;
  void perform(async () => { apply(await saveJourney(current, draft.value)); success.value = '办理步骤已保存。'; });
}
function addStep(): void {
  if (!selected.value || draft.value.length >= 40) return;
  draft.value.push({ id: crypto.randomUUID(), title: '', depends_on: [], estimate_minutes: null,
    assigned_user_id: auth.user!.id, status: 'open' });
}
function removeStep(id: string): void {
  draft.value = draft.value.filter(step => step.id !== id).map(step => ({ ...step, depends_on: step.depends_on.filter(parent => parent !== id) }));
}
function act(step: JourneyStep): void {
  const current = selected.value;
  if (current) void perform(async () => { apply(await actOnStep(current, step)); });
}
function syncSource(): void {
  const current = selected.value;
  if (current) void perform(async () => { apply(await refreshJourneySource(current)); success.value = '新增材料已加入，撤回的要求已标记；原有步骤和进度已保留，请核对。'; });
}
function preview(): void {
  const current = selected.value;
  if (current) void perform(async () => { const result = await previewJourneyPlan(current, availability.value); if (!disposed) plan.value = result; });
}
function commit(): void {
  const current = selected.value;
  const previewPlan = plan.value;
  if (!current || !previewPlan) return;
  void perform(async () => {
    const result = await scheduleJourney(current, availability.value, previewPlan);
    apply(result.journey);
    success.value = '建议时段已写入个人日程，可在日程中调整或取消。';
  });
}
function share(): void {
  const current = selected.value;
  if (!current || !shareConfirmed.value || !sharedTitle.value.trim()) return;
  void perform(async () => {
    const result = await inviteJourney(current, sharedTitle.value);
    apply(result.journey);
    if (!disposed) inviteCode.value = result.code;
    if (!disposed) inviteCopied.value = false;
  });
}
/** 邀请码只显示一次，复制按钮是为了不再靠肉眼选中整串随机字符。 */
async function copyInvite(): Promise<void> {
  if (!inviteCode.value) return;
  try {
    await navigator.clipboard.writeText(inviteCode.value);
    if (!disposed) inviteCopied.value = true;
    window.setTimeout(() => { if (!disposed) inviteCopied.value = false; }, 2400);
  } catch {
    // 浏览器拒绝剪贴板权限时保留输入框，用户仍可手动复制。
    if (!disposed) inviteCopied.value = false;
  }
}
/** 使用系统分享入口可转发到微信，邮件入口只打开草稿，仍由用户发送。 */
function inviteLink(): string {
  return `${window.location.origin}/matters/journeys#invite=${encodeURIComponent(inviteCode.value)}`;
}
async function shareInviteLink(): Promise<void> {
  if (!inviteCode.value) return;
  try {
    if (navigator.share) await navigator.share({ title: sharedTitle.value, text: '邀请你加入知序事务协作', url: inviteLink() });
    else { await navigator.clipboard.writeText(inviteLink()); success.value = '邀请链接已复制，可发送到微信。'; }
  } catch { /* 用户关闭系统分享时保留当前邀请码。 */ }
}
function emailInvite(): void {
  window.location.href = `mailto:?subject=${encodeURIComponent('知序事务协作邀请')}&body=${encodeURIComponent(inviteLink())}`;
}
function closeShare(): void {
  const current = selected.value;
  if (current) void perform(async () => { apply(await closeJourneyInvite(current)); success.value = '邀请码已关闭，已有成员仍保留访问。'; });
}
function revoke(id: string): void {
  const current = selected.value;
  if (current) void perform(async () => { apply(await revokeJourneyMember(current, id)); success.value = '成员访问已撤回，邀请码已关闭；未完成步骤可重新分配。'; });
}
function join(): void {
  if (!joinCode.value.trim() || !alias.value.trim()) return;
  void perform(async () => {
    const value = await joinJourney(joinCode.value.trim(), alias.value.trim());
    apply(value);
    joinCode.value = '';
    if (!disposed) await router.replace({ path: '/matters/journeys', query: { id: value.id } });
  });
}
function leave(): void {
  const current = selected.value;
  if (!current) return;
  void perform(async () => {
    await leaveJourney(current);
    if (disposed) return;
    selected.value = null;
    await router.replace('/matters/journeys');
    await refresh();
  });
}
function setWeekday(day: number, event: Event): void {
  const checked = readMduiChecked(event);
  const values = new Set(availability.value.weekdays);
  if (checked) values.add(day); else values.delete(day);
  availability.value.weekdays = [...values].sort((left, right) => left - right);
}
function setEstimate(step: JourneyStep, event: Event): void {
  step.estimate_minutes = readMduiNumber(event) || null;
}
function setDepends(step: JourneyStep, event: Event): void {
  step.depends_on = readMduiValues(event);
}
function message(value: unknown): string { return value instanceof ApiError ? value.message : '暂时无法完成操作，请稍后重试。'; }
function timeLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', { timeZone: availability.value.timezone, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
}
</script>

<template>
  <section class="page-section journeys-page" aria-labelledby="journeys-title">
    <header class="journeys-header">
      <div><p class="eyebrow">从通知到完成</p><h1 id="journeys-title">我的办理路线</h1><p>理清材料与下一步，再看看截止前是否来得及。</p></div>
      <div class="button-row">
        <mdui-button variant="text" @click="router.push('/matters')">返回事务</mdui-button>
        <mdui-button variant="text" @click="router.push('/matters/opportunities')">发现机会</mdui-button>
        <mdui-button variant="outlined" :disabled="busy || loading" @click="refresh">刷新进度</mdui-button>
      </div>
    </header>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <p v-if="success" class="success-text" role="status">{{ success }}</p>
    <p v-if="loading" class="helper-text" role="status">正在读取办理路线……</p>
    <div v-else class="journeys-layout">
      <aside class="journeys-sidebar">
        <form class="journey-card" @submit.prevent="create">
          <h2>从事务建立</h2>
          <mdui-select variant="outlined" label="选择待处理事务" :value="taskId" required @change="taskId = readMduiValue($event)">
            <mdui-menu-item value="">请选择</mdui-menu-item>
            <mdui-menu-item v-for="task in tasks" :key="task.id" :value="task.id">{{ task.title }}</mdui-menu-item>
          </mdui-select>
          <mdui-button type="submit" variant="filled" :disabled="busy || !taskId" :loading="busy">建立办理路线</mdui-button>
          <small>列出最近 100 项待处理事务；也可从任一事务详情建立。</small>
        </form>
        <section class="journey-card">
          <h2>我的与协作中的</h2>
          <mdui-list>
            <mdui-list-item
              v-for="item in journeys"
              :key="item.id"
              :headline="item.title"
              :description="`${item.is_owner ? '我发起' : '我参与'} · ${item.steps.filter(step => step.status === 'done').length}/${item.steps.length}`"
              :active="selected?.id === item.id"
              :disabled="busy || editing"
              rounded
              @click="router.replace({ path: '/matters/journeys', query: { id: item.id } })"
            ></mdui-list-item>
          </mdui-list>
          <p v-if="!journeys.length" class="helper-text">还没有路线，先选一件事务。</p>
          <small v-if="total > journeys.length">展示最近 {{ journeys.length }} 条，共 {{ total }} 条。</small>
        </section>
        <form class="journey-card" @submit.prevent="join">
          <h2>加入责任交接</h2>
          <p class="helper-text">邀请码由发起人在自己的办理路线里生成，作为成员你只要输入邀请码和昵称。</p>
          <mdui-text-field label="邀请码" :value="joinCode" required minlength="32" maxlength="100" autocomplete="off" @input="joinCode = readMduiValue($event)"></mdui-text-field>
          <mdui-text-field label="成员昵称" :value="alias" required maxlength="40" @input="alias = readMduiValue($event)"></mdui-text-field>
          <mdui-button type="submit" variant="outlined" :disabled="busy || !joinCode.trim() || !alias.trim()">主动加入</mdui-button>
        </form>
      </aside>

      <div v-if="selected" class="journey-main">
        <section class="journey-card">
          <div class="journeys-header">
            <div>
              <h2>{{ selected.title }}</h2>
              <p>已完成 {{ progress }}<template v-if="selected.deadline?.local_date"> · {{ selected.deadline.local_date }} {{ selected.deadline.local_time?.slice(0, 5) }} 截止</template></p>
            </div>
            <mdui-button v-if="selected.task_id" variant="text" @click="router.push(`/tasks/${selected.task_id}`)">事务详情</mdui-button>
          </div>
          <p v-if="!selected.is_owner">
            这是发起人主动共享的办理步骤；你可以确认分配给自己的部分。
            <mdui-button variant="text" :disabled="busy" @click="leave">退出协作</mdui-button>
          </p>
          <p v-if="nextStep" class="next-step"><strong>我的下一步</strong> {{ nextStep.title }}</p>
          <p v-else-if="selected.steps.every(step => step.status === 'done')" class="next-step">步骤已全部完成。<template v-if="selected.is_owner">核对结果后，可在事务详情标记完成。</template></p>
          <p v-else class="helper-text">当前没有本人可开始的步骤，可查看负责人及前置交接进度。</p>
          <div v-if="selected.source_changed" class="journey-warning">
            <p>通知要求已变化，当前路线需要重新核对。</p>
            <mdui-button variant="outlined" :disabled="busy || editing" @click="syncSource">同步要求并保留进度</mdui-button>
          </div>
          <template v-if="selected.source">
            <p v-if="selected.source.actor || selected.source.stage">办理主体：{{ selected.source.actor || '待核实' }} · 阶段：{{ selected.source.stage || '待核实' }}</p>
            <p v-if="selected.source.submission">提交方式：{{ selected.source.submission }}</p>
            <ul v-if="selected.source.unknowns.length" class="journey-warning"><li v-for="unknown in selected.source.unknowns" :key="unknown">待核实：{{ unknown }}</li></ul>
            <p v-if="selected.source.unlisted_materials.length" class="journey-warning">尚未列入步骤的来源材料：{{ selected.source.unlisted_materials.join('、') }}。请核对并同步要求。</p>
            <details v-if="selected.source.evidence.length"><summary>查看要求依据</summary><blockquote v-for="(evidence, index) in selected.source.evidence" :key="index">{{ evidence.quote }}</blockquote></details>
          </template>

          <form v-if="editing" @submit.prevent="save">
            <article v-for="(step, index) in draft" :key="step.id" class="journey-step-editor">
              <mdui-text-field label="步骤标题" :value="step.title" required maxlength="300" @input="step.title = readMduiValue($event)"></mdui-text-field>
              <div class="journey-grid">
                <mdui-text-field label="预计耗时（分钟）" type="number" min="5" max="1440" :value="step.estimate_minutes === null ? '' : String(step.estimate_minutes)" @input="setEstimate(step, $event)"></mdui-text-field>
                <mdui-select variant="outlined" label="负责人" :value="step.assigned_user_id" @change="step.assigned_user_id = readMduiValue($event)">
                  <mdui-menu-item v-if="!selected.members.some(member => member.user_id === step.assigned_user_id)" :value="step.assigned_user_id">成员已退出（保留历史）</mdui-menu-item>
                  <mdui-menu-item v-for="member in selected.members" :key="member.user_id" :value="member.user_id">{{ member.alias }}</mdui-menu-item>
                </mdui-select>
              </div>
              <mdui-select variant="outlined" multiple label="前置步骤（可多选）" :value="step.depends_on" @change="setDepends(step, $event)">
                <mdui-menu-item v-for="parent in draft.filter(item => item.id !== step.id)" :key="parent.id" :value="parent.id">{{ parent.title || '未命名步骤' }}</mdui-menu-item>
              </mdui-select>
              <mdui-button type="button" variant="text" :disabled="busy" @click="removeStep(step.id)">移除步骤</mdui-button>
              <p class="helper-text">步骤 {{ index + 1 }}</p>
            </article>
            <div class="button-row">
              <mdui-button type="button" variant="outlined" :disabled="busy || draft.length >= 40" @click="addStep">增加步骤</mdui-button>
              <mdui-button type="submit" variant="filled" :disabled="busy || !draft.length || !dirty">保存步骤</mdui-button>
              <mdui-button type="button" variant="text" :disabled="busy" @click="selectJourney(selected)">取消编辑</mdui-button>
            </div>
          </form>
          <template v-else>
            <ol class="journey-steps">
              <li v-for="step in selected.steps" :key="step.id" :class="{ 'step-done': step.status === 'done' }">
                <div>
                  <strong>{{ step.title }}</strong>
                  <p>{{ step.assignee_label }} · {{ step.estimate_minutes == null ? '耗时待填写' : `${step.estimate_minutes} 分钟` }}</p>
                  <small v-if="step.depends_on.length">前置：{{ step.depends_on.map(id => selected?.steps.find(parent => parent.id === id)?.title ?? '待核对').join('、') }}</small>
                  <small v-if="step.source_removed" class="journey-warning">来源已不包含此要求，请核对后保留或移除。</small>
                </div>
                <mdui-button v-if="step.assigned_user_id === auth.user?.id" variant="outlined" :disabled="busy" @click="act(step)">
                  {{ step.status === 'done' ? '重新打开' : '确认完成' }}
                </mdui-button>
              </li>
            </ol>
            <mdui-button v-if="selected.is_owner" variant="text" :disabled="busy" @click="editing = true; plan = null">编辑步骤、耗时与交接</mdui-button>
          </template>
        </section>

        <section v-if="selected.is_owner" class="journey-card">
          <h2>截止前，来得及吗？</h2>
          <p>比较不同空闲窗口。耗时由你确认，同伴未交接的步骤会单独提示。</p>
          <div class="journey-grid">
            <mdui-text-field label="每天从" type="time" :value="availability.start_time" @input="availability.start_time = readMduiValue($event)"></mdui-text-field>
            <mdui-text-field label="到" type="time" :value="availability.end_time" @input="availability.end_time = readMduiValue($event)"></mdui-text-field>
            <mdui-text-field label="时区" :value="availability.timezone" maxlength="100" @input="availability.timezone = readMduiValue($event)"></mdui-text-field>
          </div>
          <fieldset class="journey-weekdays">
            <legend>可用星期</legend>
            <mdui-checkbox v-for="(day, index) in ['一', '二', '三', '四', '五', '六', '日']" :key="day" :checked="availability.weekdays.includes(index + 1)" @change="setWeekday(index + 1, $event)">{{ day }}</mdui-checkbox>
          </fieldset>
          <mdui-button variant="filled" :disabled="busy || editing" @click="preview">计算建议安排</mdui-button>
          <div v-if="plan" class="journey-plan">
            <h3>{{ planLabel }}</h3>
            <p>已知待安排 {{ plan.required_minutes }} 分钟 · 窗口空闲 {{ plan.available_minutes }} 分钟 · 已预约 {{ plan.already_booked_minutes }} 分钟</p>
            <p v-if="plan.latest_start">按当前耗时与依赖倒排，最晚需在 {{ timeLabel(plan.latest_start) }} 开始；建议采用下方更早的时段，留出修改余量。</p>
            <ul v-if="plan.unresolved.length" class="journey-warning"><li v-for="item in plan.unresolved" :key="item.step_id">{{ item.title }}：{{ item.reason }}</li></ul>
            <ol><li v-for="(slot, index) in plan.slots" :key="index">{{ timeLabel(slot.start_at) }}—{{ timeLabel(slot.end_at) }} · {{ slot.title }}（{{ slot.minutes }} 分钟）</li></ol>
            <p v-for="note in plan.assumptions" :key="note" class="helper-text">{{ note }}</p>
            <mdui-button v-if="canCommit" variant="filled" :disabled="busy" @click="commit">将这些时段加入个人日程</mdui-button>
            <mdui-button variant="text" @click="router.push('/schedule')">查看、调整或取消个人日程</mdui-button>
          </div>
        </section>

        <section v-if="selected.is_owner" class="journey-card journey-invite" aria-labelledby="journey-invite-heading">
          <div class="journeys-header">
            <div>
              <h2 id="journey-invite-heading">邀请协作成员</h2>
              <p>生成邀请码后手动发给同学；对方在左侧「加入责任交接」输入邀请码和昵称即可加入。</p>
            </div>
            <mdui-chip v-if="selected.invite_active" variant="assist">邀请码有效中（1 天）</mdui-chip>
          </div>
          <div v-if="selected.members.filter(item => !item.is_owner).length" class="journey-members">
            <h3>已加入的成员</h3>
            <ul>
              <li v-for="member in selected.members.filter(item => !item.is_owner)" :key="member.user_id">
                {{ member.alias }}
                <mdui-button variant="text" :disabled="busy" @click="revoke(member.user_id)">撤回访问</mdui-button>
              </li>
            </ul>
            <p class="helper-text">成员加入后，在上方「编辑步骤」里把对应步骤分配给对方，每个人确认自己负责的部分。</p>
          </div>
          <form @submit.prevent="share">
            <mdui-text-field label="分享给成员的标题" :value="sharedTitle" required maxlength="200" placeholder="例如：竞赛材料准备" @input="sharedTitle = readMduiValue($event)"></mdui-text-field>
            <mdui-checkbox :checked="shareConfirmed" required @change="shareConfirmed = readMduiChecked($event)">
              我已检查全部步骤标题，同意向持邀请码加入的成员共享步骤、耗时、依赖与进度。
            </mdui-checkbox>
            <div class="button-row">
              <mdui-button type="submit" variant="filled" :disabled="busy || editing || !shareConfirmed || !sharedTitle.trim()">
                {{ selected.invite_active ? '重新生成邀请码' : '生成邀请码（1 天有效）' }}
              </mdui-button>
              <mdui-button v-if="selected.invite_active" type="button" variant="outlined" :disabled="busy" @click="closeShare">关闭邀请码</mdui-button>
            </div>
          </form>
          <div v-if="inviteCode" class="journey-invite-code">
            <mdui-text-field label="邀请码（复制后发给成员）" :value="inviteCode" readonly></mdui-text-field>
            <mdui-button type="button" variant="outlined" @click="copyInvite">{{ inviteCopied ? '已复制' : '复制邀请码' }}</mdui-button>
            <mdui-button type="button" variant="text" @click="shareInviteLink">分享邀请链接</mdui-button>
            <mdui-button type="button" variant="text" @click="emailInvite">通过邮件邀请</mdui-button>
          </div>
          <p v-else-if="selected.invite_active" class="helper-text">
            邀请码已生成过，出于安全只显示一次。如果当时没有保存，请重新生成一个。
          </p>
        </section>
        <JourneyCollaboration :key="selected.id" :journey-id="selected.id" />
        <JourneyPublications :key="selected.id + ':' + selected.version" :journey-id="selected.id" :version="selected.version" :is-owner="selected.is_owner" @changed="refresh" />
      </div>
      <section v-else class="journey-card">
        <h2>先选一件要办的事</h2>
        <p>通知中的材料可以自动列入清单；依赖与耗时由你确认。</p>
        <mdui-button variant="outlined" @click="router.push('/matters')">创建或查看事务</mdui-button>
      </section>
    </div>
  </section>
</template>

<style scoped>
.journeys-page a{color:rgb(var(--mdui-color-primary,54 91 155));text-decoration:none;font-weight:500}
.next-step{padding:12px 14px;background:#eef3fc;border-left:3px solid #526fa5;border-radius:8px}
.next-step strong{margin-right:10px}
.journey-card>small{display:block;margin-top:10px}
.journeys-header,.button-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.journeys-layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:20px}
.journeys-sidebar,.journey-main{display:flex;flex-direction:column;gap:18px;min-width:0}
.journey-card{padding:22px;border:1px solid var(--border-color,#ddd);border-radius:18px;background:var(--surface-color,#fff);min-width:0;overflow-wrap:anywhere}
.journey-card h2{margin-top:0;font-size:1.2rem}
.journey-card mdui-text-field,.journey-card mdui-select{display:block;margin:10px 0}
.journey-card mdui-button{margin-top:8px}
.journey-card mdui-list-item{--mdui-typescale-label-large-size:12px}
.journey-steps{padding-left:24px}
.journey-steps li{padding:15px 0;border-bottom:1px solid #eee;display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.journey-steps li>div{margin-bottom:0;min-width:0;flex:1}
.journey-steps small{display:block}
.journey-step-editor{padding:14px 0;border-bottom:1px solid #ddd;margin-bottom:16px}
.journey-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}
.journey-warning{background:#fff6e4;color:#70521e;padding:10px;border-radius:8px}
.step-done strong{text-decoration:line-through;opacity:.6}
.journey-weekdays{display:flex;gap:14px;flex-wrap:wrap;border:0;padding:6px 0 14px}
.journey-plan{margin-top:18px}
.journey-plan a{display:block;margin-top:12px}
/* 邀请码是这条路线唯一的对外凭证，生成、复制和有效期都要一眼可见。 */
.journey-invite h2{margin-bottom:4px}
.journey-invite .journeys-header{align-items:flex-start;margin-bottom:10px}
.journey-invite .journeys-header p{margin:0;color:var(--app-on-surface-variant,#5d6777);font-size:13px;line-height:1.7}
.journey-members h3{margin:6px 0;font-size:.95rem}
.journey-members ul{margin:0;padding-left:18px;line-height:1.9}
.journey-invite-code{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;margin-top:8px;padding:12px;border:1px dashed #b9c8e0;border-radius:12px;background:#f5f8ff}
.journey-invite-code mdui-text-field{flex:1;min-width:220px;margin:0}
.journey-invite-code mdui-button{margin:0}
@media (max-width:800px){.journeys-layout{grid-template-columns:minmax(0,1fr)}.journey-card{padding:15px}.journey-steps li{flex-direction:column}.journey-grid{grid-template-columns:minmax(0,1fr)}.journeys-header{align-items:stretch;flex-direction:column}.button-row{justify-content:flex-start}}
</style>
