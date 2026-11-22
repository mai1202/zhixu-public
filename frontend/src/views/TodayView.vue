<script setup lang="ts">
import { getTodayActions, getSourceHealth, type TodayActions, type SourceHealth } from '../api/innovation';
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { listTasks, setTaskStatus, type TaskSummary } from '../api/business';
import { ApiError } from '../api/client';
import { listPairedDevices, type PairedDevice } from '../api/devices';
import { listSchedule, type ScheduleItem, type ScheduleResponse } from '../api/timetable';
import { getNotificationCounts, listNotifications, type NotificationMessage } from '../api/notifications';
import AppIcon from '../components/AppIcon.vue';
import AgentRuntimeAlert from '../components/AgentRuntimeAlert.vue';
import OnboardingPrompt from '../components/OnboardingPrompt.vue';
import NotificationSuggestions from '../components/NotificationSuggestions.vue';
import { formatRelativeDateTime } from '../utils/datetime';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const actions = ref<TodayActions | null>(null);
const sourceHealth = ref<SourceHealth | null>(null);
const seedPromptKey = `agent-seeded-prompt:${useAuthStore().user?.id ?? ''}`;
const schedule = ref<ScheduleResponse | null>(null);
const tasks = ref<TaskSummary[]>([]);
const notifications = ref<NotificationMessage[]>([]);
const unreadCount = ref(0);
const pairedDevices = ref<PairedDevice[]>([]);
const deviceStatusReady = ref(false);
const loading = ref(false);
const errorMessage = ref('');
const taskBusyId = ref('');
const now = ref(new Date());
const today = computed(() => localDate(now.value));
// 计时器只维护本页时钟，跨天或从后台回来时重新读取业务数据。
let clockTimer: number | undefined;
let disposed = false;
let refreshVersion = 0;

const weekday = computed(() => new Intl.DateTimeFormat('zh-CN', { weekday: 'long' }).format(now.value));
const dateLabel = computed(() => new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric' }).format(now.value));
const greeting = computed(() => {
  const hour = now.value.getHours();
  if (hour < 6) return '夜深了';
  if (hour < 11) return '早上好';
  if (hour < 14) return '中午好';
  if (hour < 18) return '下午好';
  return '晚上好';
});

const todaysItems = computed(() => [...(schedule.value?.items ?? [])]
  .filter((item) => item.local_date === today.value)
  .sort((left, right) => timeValue(left.start_at) - timeValue(right.start_at)));
const todaysDeadlines = computed(() => (schedule.value?.deadlines ?? [])
  .filter((item) => item.local_date === today.value));
const openTasks = computed(() => tasks.value
  .filter((task) => task.status === 'open')
  .sort((left, right) => dueValue(left) - dueValue(right)));
const upNext = computed(() => todaysItems.value.find((item) => endValue(item) > now.value.getTime()) ?? null);
const isOngoing = computed(() => Boolean(upNext.value?.start_at && scheduleTime(upNext.value.start_at) <= now.value.getTime()));
// 通知雷达使用与通知页相同的阅读入口和已读状态，不拿候选充当通知。
const notificationPreview = computed(() => notifications.value.slice(0, 3));
const overdueTasks = computed(() => openTasks.value.filter((task) => {
  const dueDate = task.due?.local_date;
  return Boolean(dueDate && dueDate < today.value);
}));
const dueTodayTasks = computed(() => openTasks.value.filter((task) => task.due?.local_date === today.value));
const activeDevices = computed(() => pairedDevices.value.filter((device) => device.status === 'active'));
const pendingDevices = computed(() => pairedDevices.value.filter((device) => device.status === 'pending'));
const showDeviceSetup = computed(() => deviceStatusReady.value && activeDevices.value.length === 0);
const nextSuggestion = computed(() => {
  if (unreadCount.value) {
    return {
      title: `还有 ${unreadCount.value} 条未读通知`,
      detail: '打开通知查看 Agent 整理的摘要、原文与行动建议。',
    };
  }
  if (overdueTasks.value.length) {
    return {
      title: `先处理 ${overdueTasks.value.length} 个逾期事项`,
      detail: '这些事项的截止日期已经过去，建议优先重新安排时间。',
    };
  }
  if (upNext.value) {
    return {
      title: isOngoing.value ? `${upNext.value.title} 正在进行` : `准备下一项：${upNext.value.title}`,
      detail: `${formatTime(upNext.value.start_at)}${upNext.value.end_at ? `–${formatTime(upNext.value.end_at)}` : ''} · ${itemSubtitle(upNext.value)}`,
    };
  }
  if (openTasks.value.length) {
    return {
      title: `还有 ${openTasks.value.length} 个待办可以安排`,
      detail: '今天暂无后续课程，可以让 Agent 根据紧急程度安排剩余时间。',
    };
  }
  return {
    title: '今天暂时没有紧急事项',
    detail: '可以把长期目标拆成一个小步骤，或者提前准备接下来的任务。',
  };
});
const agentPrompt = computed(() => {
  const events = todaysItems.value.map((item) => `${formatTime(item.start_at)} ${item.title}（${itemSubtitle(item)}）`);
  const taskTitles = openTasks.value.slice(0, 8).map((task) => `${task.title.slice(0, 120)}（${formatDue(task)}）`);
  const unreadTitles = notifications.value.filter((item) => !item.read).slice(0, 5).map((item) => item.title.slice(0, 160));
  return [
    '请根据以下今日上下文，帮我制定今天剩余时间的具体行动计划。',
    `今天安排：${events.join('；') || '无'}`,
    `未完成待办：${taskTitles.join('；') || '无'}`,
    `未读通知：${unreadTitles.join('；') || '无'}`,
    '请先给优先级，再给带时间的安排，并指出还需要我确认的信息。',
  ].join('\n');
});

function openAgentPlan(): void {
  // 上下文只通过当前标签页的一次性草稿传递，避免把课程和通知摘要暴露在地址栏。
  sessionStorage.setItem(seedPromptKey, agentPrompt.value);
  void router.push('/agent');
}

onMounted(() => {
  void refresh();
  clockTimer = window.setInterval(updateClock, 30_000);
  document.addEventListener('visibilitychange', onVisibilityChange);
});

onUnmounted(() => {
  disposed = true;
  refreshVersion += 1;
  if (clockTimer !== undefined) window.clearInterval(clockTimer);
  document.removeEventListener('visibilitychange', onVisibilityChange);
});

function updateClock(): void {
  if (disposed) return;
  const previousDate = today.value;
  now.value = new Date();
  if (today.value !== previousDate) void refresh();
}

function onVisibilityChange(): void {
  if (document.visibilityState === 'visible') void refresh();
}

async function refresh(): Promise<void> {
  if (disposed) return;
  const version = ++refreshVersion;
  loading.value = true;
  errorMessage.value = '';
  now.value = new Date();
  const requestedDate = today.value;
  try {
    const [scheduleResult, taskResult, notificationCounts, deviceItems, notificationResult] = await Promise.all([
      listSchedule(requestedDate, requestedDate),
      listTasks(),
      // 未读总数来自同一通知邮箱，不再把旧候选误报为“待确认通知”。
      getNotificationCounts(),
      // 设备引导不能拖垮首页其它数据；单独容错，读取成功后再决定是否展示。
      listPairedDevices().then((result) => result.items).catch(() => null),
      listNotifications('inbox', ''),
    ]);
    // 跨天、重复刷新和离页后的旧响应不能覆盖最新页面。
    if (disposed || version !== refreshVersion) return;
    schedule.value = scheduleResult;
    // 增强面板独立容错，不能因健康接口失败阻塞原有今日课程和任务。
    void Promise.allSettled([getTodayActions(), getSourceHealth()]).then(([actionResult, healthResult]) => {
      if (disposed || version !== refreshVersion) return;
      if (actionResult.status === 'fulfilled') actions.value = actionResult.value;
      if (healthResult.status === 'fulfilled') sourceHealth.value = healthResult.value;
    });
    tasks.value = taskResult.items;
    unreadCount.value = notificationCounts.unread;
    notifications.value = notificationResult.items;
    if (deviceItems) {
      pairedDevices.value = deviceItems;
      deviceStatusReady.value = true;
    }
  } catch (error) {
    if (disposed || version !== refreshVersion) return;
    errorMessage.value = error instanceof ApiError ? error.message : '今天的信息暂时无法加载。';
  } finally {
    if (!disposed && version === refreshVersion) loading.value = false;
  }
}

async function completeTask(task: TaskSummary): Promise<void> {
  if (taskBusyId.value) return;
  taskBusyId.value = task.id;
  errorMessage.value = '';
  try {
    await setTaskStatus(task, 'complete');
    await refresh();
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '待办没有更新，请稍后重试。';
  } finally {
    taskBusyId.value = '';
  }
}

function localDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function timeValue(value: string | null): number {
  return value ? scheduleTime(value) : Number.MAX_SAFE_INTEGER;
}

function endValue(item: ScheduleItem): number {
  if (item.end_at) return scheduleTime(item.end_at);
  return Number.MAX_SAFE_INTEGER;
}

function scheduleTime(value: string): number {
  // 日程接口在单日查询中可能返回 HH:mm，也可能返回完整 ISO 时间。
  if (/^\d{2}:\d{2}$/.test(value)) return Date.parse(`${today.value}T${value}:00`);
  return Date.parse(value);
}

function dueValue(task: TaskSummary): number {
  const value = task.due?.local_date;
  if (!value) return Number.MAX_SAFE_INTEGER;
  return Date.parse(`${value}T${task.due?.local_time ?? '23:59'}:00`);
}

function formatTime(value: string | null): string {
  if (!value) return '全天';
  if (/^\d{2}:\d{2}$/.test(value)) return value;
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(new Date(value));
}

function formatDue(task: TaskSummary): string {
  const date = task.due?.local_date;
  if (!date) return '未设置期限';
  if (date < today.value) return `已逾期 · ${date.slice(5)}`;
  if (date === today.value) return task.due?.local_time ? `今天 ${task.due.local_time}` : '今天截止';
  const tomorrow = new Date(`${today.value}T12:00:00`);
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (date === localDate(tomorrow)) return task.due?.local_time ? `明天 ${task.due.local_time}` : '明天截止';
  return `${date.slice(5)} 截止`;
}

function itemSubtitle(item: ScheduleItem): string {
  if (item.item_type === 'course') {
    return [item.room, item.teachers?.length ? item.teachers.join('、') : ''].filter(Boolean).join(' · ') || '课程';
  }
  return item.kind === 'all_day' ? '个人安排 · 全天' : '个人安排';
}
</script>

<template>
  <section class="page-section today-page" aria-labelledby="today-heading">
    <header class="today-heading">
      <div class="today-heading-copy">
        <div class="today-date-line">
          <span class="today-date-chip">{{ dateLabel }}</span>
          <span>{{ weekday }}</span>
        </div>
        <h1 id="today-heading">{{ greeting }}</h1>
        <p class="today-summary">
          <span>{{ todaysItems.length }} 项安排</span>
          <span>{{ openTasks.length }} 个待办</span>
          <span>{{ unreadCount }} 条未读</span>
        </p>
      </div>
      <div class="today-heading-actions">
        <mdui-tooltip content="刷新今日信息">
          <mdui-button-icon aria-label="刷新今日信息" :disabled="loading" @click="refresh">
            <AppIcon name="refresh"></AppIcon>
          </mdui-button-icon>
        </mdui-tooltip>
      </div>
    </header>

    <div v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</div>
    <div v-if="loading && !schedule" class="today-loading" role="status">
      <mdui-circular-progress></mdui-circular-progress>
      <span>正在整理今天的信息……</span>
    </div>

    <div class="today-dashboard">
      <section class="today-block" aria-labelledby="today-timeline-heading">
        <div class="section-heading">
          <div>
            <h2 id="today-timeline-heading">今天</h2>
          </div>
          <RouterLink to="/schedule">全部日程</RouterLink>
        </div>
        <ol v-if="todaysItems.length" class="today-timeline">
          <li v-for="item in todaysItems" :key="item.id">
            <RouterLink
              class="today-timeline-link"
              :to="item.item_type === 'course'
                ? { path: '/courses', query: { focus: item.id, date: item.local_date } }
                : { path: '/schedule', query: { focus: item.id, date: item.local_date } }"
            >
              <span class="today-timeline-icon"><AppIcon :name="item.item_type === 'course' ? 'calendar_month' : 'event'" /></span>
              <span class="today-timeline-copy">
                <strong>{{ item.title }}</strong>
                <small>{{ itemSubtitle(item) }}</small>
              </span>
              <time :datetime="item.start_at ?? undefined">{{ formatTime(item.start_at) }}<template v-if="item.end_at">–{{ formatTime(item.end_at) }}</template></time>
              <AppIcon name="chevron_right"></AppIcon>
            </RouterLink>
          </li>
        </ol>
        <p v-else class="today-empty">今天没有课程或个人安排。</p>
        <div v-if="todaysDeadlines.length" class="today-inline-deadlines">
          <strong>今天截止</strong>
          <span v-for="deadline in todaysDeadlines" :key="deadline.id">{{ deadline.title }}</span>
        </div>
      </section>

      <mdui-card class="today-action-card" variant="outlined" aria-label="今日行动清单">
        <div class="today-action-heading">
          <div>
            <h2>{{ openTasks.length ? `${openTasks.length} 项待办` : '待办已经清空' }}</h2>
            <p v-if="openTasks.length">
              {{ overdueTasks.length ? `${overdueTasks.length} 项已逾期` : '按期限从近到远' }}
              <template v-if="dueTodayTasks.length"> · {{ dueTodayTasks.length }} 项今天截止</template>
            </p>
            <p v-else>可以先检查日程，或让助手规划剩余时间。</p>
          </div>
          <RouterLink class="today-action-all" to="/matters">全部事务</RouterLink>
        </div>

        <div v-if="openTasks.length" class="today-action-list">
          <div v-for="task in openTasks.slice(0, 2)" :key="task.id" class="today-action-row">
            <RouterLink :to="`/tasks/${task.id}`" class="today-action-copy">
              <strong>{{ task.title }}</strong>
              <small :data-overdue="Boolean(task.due?.local_date && task.due.local_date < today)">{{ formatDue(task) }}</small>
            </RouterLink>
            <mdui-button-icon
              class="today-task-complete"
              :aria-label="`标记完成：${task.title}`"
              :loading="taskBusyId === task.id"
              :disabled="Boolean(taskBusyId)"
              @click="completeTask(task)"
            >
              <AppIcon name="check"></AppIcon>
            </mdui-button-icon>
          </div>
        </div>

      </mdui-card>
    </div>

    <section v-if="actions" class="today-action-panel" aria-label="下一步行动">
      <header class="today-action-panel-heading">
        <h2>下一步行动</h2>
        <RouterLink class="app-link" to="/matters/planning">统一安排</RouterLink>
      </header>
      <ul class="today-next-list"><li v-for="item in actions.items.slice(0, 5)" :key="item.id">
        <RouterLink :to="item.href" class="today-next-link"><AppIcon :name="item.kind === 'schedule' ? 'calendar_month' : 'checklist'" />
          <span><strong>{{ item.title }}</strong><small>{{ item.reason }}<template v-if="item.start_at"> · {{ formatRelativeDateTime(Date.parse(item.start_at) / 1000) }}</template></small></span>
          <AppIcon name="chevron_right" />
        </RouterLink></li></ul>
      <p v-if="!actions.items.length" class="helper-text">暂时没有需要处理的下一步行动。</p>
    </section>

    <NotificationSuggestions @changed="refresh" />
    <div class="today-columns">
      <div class="today-side-column">
        <section class="today-radar" aria-labelledby="today-radar-heading">
          <div class="section-heading">
            <div>
              <h2 id="today-radar-heading">最近通知</h2>
            </div>
            <RouterLink to="/inbox">通知中心</RouterLink>
          </div>
          <div v-if="notificationPreview.length" class="today-radar-list">
            <RouterLink
              v-for="item in notificationPreview"
              :key="item.id"
              :to="`/inbox/${encodeURIComponent(item.id)}`"
              class="today-radar-row"
              :data-unread="!item.read"
            >
              <span v-if="!item.read" class="notification-read-dot" aria-label="未读"></span>
              <span class="today-radar-copy">
                <strong class="notification-title" :data-read="item.read">{{ item.title }}</strong>
                <small>{{ item.summary || '打开查看通知原文与行动建议' }}</small>
              </span>
              <AppIcon name="chevron_right"></AppIcon>
            </RouterLink>
          </div>
          <p v-else class="today-radar-empty">
            <AppIcon name="notifications_off"></AppIcon>
            新通知会在 Agent 整理后显示在这里，打开即可阅读。
          </p>
        </section>
      </div>
    </div>
    <div class="today-support">
      <OnboardingPrompt />
      <AgentRuntimeAlert />
      <mdui-card
        v-if="showDeviceSetup"
        class="today-device-setup"
        variant="filled"
        aria-labelledby="today-device-setup-heading"
      >
        <div class="today-device-setup-main">
          <span class="today-device-setup-icon" aria-hidden="true"><AppIcon name="computer"></AppIcon></span>
          <div>
            <span class="today-device-setup-kicker">还差一步开始自动整理</span>
            <h2 id="today-device-setup-heading">
              {{ pendingDevices.length ? '采集设备等待核对' : '连接 Windows 采集设备' }}
            </h2>
            <p>
              {{ pendingDevices.length
                ? '设备已经提交配对，确认设备并选择允许同步的会话后才会开始上传。'
                : '连接后，知序才能读取你授权的群消息，自动整理通知、待办和日程。' }}
            </p>
          </div>
          <mdui-button
            variant="filled"
            @click="router.push({ path: '/settings/devices' })"
          >
            {{ pendingDevices.length ? '去核对设备' : '去连接设备' }}
          </mdui-button>
        </div>
        <div class="today-device-setup-steps" aria-label="设备配置步骤">
          <span><b>1</b>生成一次性配对码</span>
          <span><b>2</b>在 Windows 采集端粘贴</span>
          <span><b>3</b>核对设备并勾选会话</span>
          <small>配置完成前，此提醒会一直保留在首页。</small>
        </div>
      </mdui-card>
      <section class="today-agent-cue today-action-agent" aria-label="Agent 建议">
        <span class="today-agent-mark"><AppIcon name="smart_toy"></AppIcon></span>
        <div>
          <span class="today-agent-label">Agent 建议</span>
          <strong>{{ nextSuggestion.title }}</strong>
          <p>{{ nextSuggestion.detail }}</p>
        </div>
        <mdui-button variant="text" @click="openAgentPlan">制定计划</mdui-button>
      </section>
      <div v-if="actions" class="today-diagnostics">
        <details v-if="actions.changes.length"><summary>最近变化与处理结果</summary><p v-for="item in actions.changes" :key="item.id">{{ item.summary }} · {{ item.status }}</p></details>
        <details v-if="sourceHealth"><summary>来源同步状态</summary>
          <p v-for="device in sourceHealth.devices" :key="device.id">{{ device.name }}：{{ device.status === 'current' ? '近期有成功回执' : device.status === 'delayed' ? '同步延迟' : '尚无成功观测' }}<span v-if="device.last_success"> · {{ new Date(device.last_success * 1000).toLocaleString() }}</span></p>
          <p>通知整理：等待{{ sourceHealth.notification_processing.pending }}项，失败{{ sourceHealth.notification_processing.failed }}项</p>
          <p>附件：{{ Object.values(sourceHealth.attachments ?? {}).reduce((sum, count) => sum + count, 0) }}份，解析状态可在原通知查看。</p>
          <p>邮件轮询：{{ sourceHealth.mail.poll_status === 'current' ? '近期检查成功' : sourceHealth.mail.poll_status === 'failed' ? '最近检查失败' : sourceHealth.mail.poll_status === 'delayed' ? '检查延迟' : '尚无成功观测' }}<span v-if="sourceHealth.mail.last_success"> · {{ new Date(sourceHealth.mail.last_success * 1000).toLocaleString() }}</span></p>
          <p>官网关注：{{ sourceHealth.watches.length }}项；{{ sourceHealth.watches.filter(item => item.status === 'delayed').length }}项检查延迟。</p>
          <p>{{ sourceHealth.coverage_note }}</p>
        </details>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 行动与来源诊断沿用今日页面色彩，长理由在小屏自然换行。 */
.today-action-panel { overflow-wrap: anywhere; }
/* 行动区复用站内配色与卡片节奏，让整行成为清晰的可点击入口。 */
.today-action-panel h2 { font-size: 20px; margin: 0 0 12px; }
.today-next-list { list-style: none; margin: 16px 0; padding: 0; display: grid; gap: 8px; }
.today-next-link { display: flex; align-items: center; gap: 12px; padding: 14px; border-radius: 12px; background: var(--app-surface-container, #f0f3fb); color: var(--app-on-surface, #202632); text-decoration: none; }
.today-next-link:hover { background: #e8eefb; }
.today-next-link span { flex: 1; min-width: 0; }
.today-next-link strong { display: block; font-size: 16px; font-weight: 500; }
.today-next-link small { display: block; margin-top: 5px; color: var(--app-on-surface-variant, #596579); font-size: 14px; line-height: 1.6; }
.today-diagnostics details { border-top: 1px solid var(--app-outline, #dfe3ec); padding-top: 12px; margin-top: 12px; font-size: 14px; line-height: 1.7; }
.today-diagnostics summary { cursor: pointer; font-weight: 500; }
</style>
