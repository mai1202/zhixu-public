<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { getInboxItem, undoChange } from '../api/business';
import { ApiError } from '../api/client';
import { statusLabel } from '../api/labels';
import { createDemoNotification, getAgentProgress, getDashboard, listOperations, listReminders, listReports, type AgentProgress, type DashboardData, type OperationItem, type ReminderItem, type ReportItem } from '../api/ops';
import AppIcon from '../components/AppIcon.vue';
import ModelBillingPanel from '../components/agent/ModelBillingPanel.vue';
import SingleChoiceSegmented from '../components/SingleChoiceSegmented.vue';
import { sourceSubmission } from '../api/submission';
import { useAuthStore } from '../stores/auth';
import { readMduiValue } from '../utils/mdui';

const dashboard = ref<DashboardData | null>(null);
const operations = ref<OperationItem[]>([]);
const reports = ref<ReportItem[]>([]);
const reminders = ref<ReminderItem[]>([]);
const loading = ref(false);
const busyId = ref('');
const errorMessage = ref('');
const successMessage = ref('');
const actorFilter = ref<'all' | 'agent' | 'user' | 'system'>('all');
const demoText = ref('');
const demoBusy = ref(false);
const demoState = ref('');
const demoResult = ref<{ title: string; summary: string } | null>(null);
const demoProgress = ref<AgentProgress['events']>([]);
const demoAgentState = ref('');
const auth = useAuthStore();
let disposed = false;
let refreshVersion = 0;
let pollTimer: number | undefined;
let finishPollWait: (() => void) | undefined;

const visibleOperations = computed(() => operations.value.filter((item) => (
  actorFilter.value === 'all' || item.actor_type === actorFilter.value
)));
const upcomingReminders = computed(() => reminders.value
  .filter((item) => ['scheduled', 'queued'].includes(item.status))
  .sort((left, right) => left.scheduled_at_epoch - right.scheduled_at_epoch)
  .slice(0, 6));
const latestReport = computed(() => reports.value[0] ?? null);

onMounted(() => void refresh());
onUnmounted(() => {
  disposed = true;
  refreshVersion += 1;
  if (pollTimer !== undefined) window.clearTimeout(pollTimer);
  finishPollWait?.();
});

async function refresh(): Promise<void> {
  if (disposed) return;
  const version = ++refreshVersion;
  loading.value = true;
  errorMessage.value = '';
  try {
    const [dashboardResult, operationResult, reportResult, reminderResult] = await Promise.all([
      getDashboard(), listOperations(80), listReports(10), listReminders(80),
    ]);
    if (disposed || version !== refreshVersion) return;
    dashboard.value = dashboardResult;
    operations.value = operationResult.items;
    reports.value = reportResult.items;
    reminders.value = reminderResult.items;
  } catch (error) {
    if (!disposed && version === refreshVersion) setError(error, '工作记录暂时无法加载。');
  } finally {
    if (!disposed && version === refreshVersion) loading.value = false;
  }
}

async function undo(item: OperationItem): Promise<void> {
  if (!item.change_set_id || busyId.value) return;
  if (!window.confirm(`撤销这次操作？\n${item.summary}`)) return;
  busyId.value = item.id;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    await undoChange(item.change_set_id);
    successMessage.value = '这次变更已撤销；若对象后来又被修改，系统会保留冲突保护。';
    await refresh();
  } catch (error) {
    setError(error, '这次变更不能安全撤销。');
  } finally {
    busyId.value = '';
  }
}

async function runDemo(): Promise<void> {
  const text = demoText.value.trim();
  if (!text || demoBusy.value || disposed) return;
  demoBusy.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  demoState.value = '正在提交到正式通知处理链路…';
  demoProgress.value = [];
  demoAgentState.value = '';
  let persisted = false;
  try {
    const title = '现场测试通知';
    const submission = await sourceSubmission(`demo:${auth.user?.id ?? ''}`, { text, title });
    if (disposed) return;
    const created = await createDemoNotification(text, title, submission.key);
    submission.complete();
    persisted = true;
    if (disposed) return;
    demoState.value = '材料已持久化，正在等待提取与 Agent 核验…';
    demoResult.value = {
      title: created.source.title,
      summary: created.change_set.summary,
    };
    let settled = false;
    // 现场模型可能超过 20 秒；仍轮询到约一分钟，退出时明确说明后台继续运行。
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise<void>((resolve) => {
        finishPollWait = resolve;
        pollTimer = window.setTimeout(() => { finishPollWait = undefined; resolve(); }, 1500);
      });
      if (disposed) return;
      const [detail, progress] = await Promise.all([
        getInboxItem(created.change_set.id),
        getAgentProgress(created.source.id),
      ]);
      if (disposed) return;
      demoProgress.value = progress.events;
      demoAgentState.value = progress.status === 'not_queued'
        ? '自动托管未启用；通知已按现有规则直接处理。'
        : progress.status === 'succeeded'
          ? 'Agent 工具链已结束。'
          : `Agent 运行状态：${statusLabel(progress.status)}`;
      demoResult.value = {
        title: detail.source?.title ?? created.source.title,
        summary: detail.change_set.summary,
      };
      demoState.value = progress.status === 'failed'
        ? 'Agent 执行未完成，通知原文和具体错误已保留。'
        : progress.status === 'succeeded' || progress.status === 'not_queued'
          ? '处理已完成，可查看通知与实际写入记录。'
          : '通知正在处理，可查看下方 Agent 工具进度。';
      if (
        progress.status === 'not_queued'
        || progress.status === 'succeeded'
        || progress.status === 'failed'
      ) {
        settled = true;
        await refresh();
        break;
      }
    }
    if (!settled) {
      demoState.value = '仍在后台处理；可刷新本页继续查看，已保存的输入不会丢失。';
    }
  } catch (error) {
    if (disposed) return;
    if (persisted) demoState.value = '通知原文已保存，进度暂时无法读取；可刷新工作台或到通知页查看。';
    else {
      setError(error, '现场测试通知未能进入处理链路。');
      demoState.value = '';
    }
  } finally {
    if (!disposed) demoBusy.value = false;
  }
}

function setError(error: unknown, fallback: string): void {
  if (disposed) return;
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
}

function actorLabel(value: string): string {
  return ({ agent: 'Agent', user: '我', system: '系统' } as Record<string, string>)[value] ?? value;
}

function actionLabel(value: string): string {
  const labels: Record<string, string> = {
    'task.create': '创建任务',
    'task.update': '更新任务',
    'task.completed': '完成任务',
    'task.open': '重开任务',
    'notice.create': '创建通知',
    'notice.update': '修正通知',
    'notice.snapshot': '归档通知',
    'notice.archive': '归档通知',
    'course.create': '新增课程',
    'course.update': '修改课程系列',
    'course.archive': '归档课程',
    'course.restore': '恢复课程',
    'course.makeup': '新增补课',
    'course.cancel': '取消单次课程',
    'course.reschedule': '调整单次课程',
    'course.room_change': '调整课程地点',
    'course.undo': '撤销课程变更',
    'entity.link': '建立关联',
    'entity.unlink': '解除关联',
    'source.deduplicate': '合并重复通知',
    'report.daily.generate': '生成日报',
    'report.weekly.generate': '生成周报',
  };
  return labels[value] ?? value;
}

function timestamp(epoch: number): string {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(new Date(epoch * 1000));
}

function objectHref(item: OperationItem): string {
  if (item.object_type === 'notice') return `/inbox?panel=notices&notice=${item.object_id}`;
  if (item.object_type === 'task') return `/tasks/${item.object_id}`;
  if (item.object_type === 'course_occurrence' || item.object_type === 'schedule_entry') {
    const date = [item.after.local_date, item.before.local_date]
      .find((value): value is string => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value));
    const query = new URLSearchParams({ focus: item.object_id });
    // 历史对象可能不在今天；把本地日期一起带入，页面才能直接定位该实例。
    if (date) query.set('date', date);
    return `/schedule?${query.toString()}`;
  }
  if (item.object_type === 'course_series') return '/courses';
  if (item.object_type === 'activity_report') return `/settings/mail?report=${item.object_id}`;
  return '/today';
}

function reportStatus(value: string): string {
  return ({ generated: '已生成', queued: '待投递', sent: '服务已接受', failed: '发送失败' } as Record<string, string>)[value] ?? value;
}

</script>

<template>
  <section class="page-section activity-page" aria-labelledby="activity-heading">
    <header class="activity-header">
      <div>
        <p class="eyebrow">自动运行 · 可追溯</p>
        <h1 id="activity-heading">Agent 工作台</h1>
        <p>查看通知、任务和课程的真实变更，以及提醒与报告的运行状态。</p>
      </div>
      <mdui-tooltip content="刷新工作台">
        <mdui-button-icon aria-label="刷新工作台" :disabled="loading" @click="refresh">
          <AppIcon name="refresh"></AppIcon>
        </mdui-button-icon>
      </mdui-tooltip>
    </header>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <ModelBillingPanel />

    <section v-if="dashboard" class="activity-metrics" aria-label="运行指标">
      <article>
        <span>接收材料</span>
        <strong>{{ dashboard.metrics.notices_processed }}</strong>
        <small>{{ dashboard.metrics.notices_active }} 条当前有效</small>
      </article>
      <article>
        <span>开放任务</span>
        <strong>{{ dashboard.metrics.tasks_open }}</strong>
        <small>{{ dashboard.metrics.tasks_overdue }} 个逾期 · 更新 {{ dashboard.metrics.task_updates ?? 0 }} 次</small>
      </article>
      <article>
        <span>课程调整</span>
        <strong>{{ dashboard.metrics.course_changes }}</strong>
        <small>含单次调整与课程系列维护</small>
      </article>
      <article>
        <span>去重合并</span>
        <strong>{{ dashboard.metrics.deduplicated }}</strong>
        <small>重复来源与任务合并</small>
      </article>
      <article>
        <span>Agent 操作占比</span>
        <strong>{{ dashboard.metrics.automation_rate }}%</strong>
        <small>{{ dashboard.metrics.agent_actions }} 次 Agent · {{ dashboard.metrics.manual_interventions }} 次用户操作</small>
      </article>
      <article>
        <span>主动提醒</span>
        <strong>{{ dashboard.metrics.reminders_sent }}</strong>
        <small>{{ dashboard.metrics.reminders_pending }} 条已安排</small>
      </article>
      <article>
        <span>报告</span>
        <strong>{{ dashboard.metrics.reports_generated }}</strong>
        <small>{{ dashboard.metrics.mail_accepted }} 封邮件被服务接受</small>
      </article>
      <article>
        <span>运行失败</span>
        <strong>{{ dashboard.metrics.job_failures }}</strong>
        <small>入队至完成均值 {{ dashboard.metrics.average_processing_seconds ?? '—' }} 秒 · {{ dashboard.metrics.processing_sample_count ?? 0 }} 个样本</small>
      </article>
    </section>

    <!-- 指标口径来自 API，与统计一起更新，方便用户和比赛演示核验。 -->
    <details v-if="dashboard?.metric_definitions" class="inline-message">
      <summary>指标如何计算</summary>
      <p v-for="(definition, key) in dashboard.metric_definitions" :key="key">{{ definition }}</p>
    </details>

    <section v-if="dashboard" class="activity-agent-state">
      <span class="activity-agent-icon" :data-online="dashboard.agent_status.automatic_notifications">
        <AppIcon name="smart_toy"></AppIcon>
      </span>
      <div>
        <strong>{{ dashboard.agent_status.automatic_notifications
          ? dashboard.agent_status.model_ready ? '自动通知处理已启用' : '自动通知已开启 · 等待配置主助手模型'
          : '通知托管未启用 · 主动扫描仅生成本地建议' }}</strong>
        <p v-if="dashboard.agent_status.last_action">
          最近操作：{{ dashboard.agent_status.last_action.summary }}
        </p>
        <p v-else>还没有 Agent 自动操作记录。</p>
      </div>
      <span class="activity-running">
        {{ dashboard.agent_status.active_insights ?? 0 }} 条主动建议 · {{ dashboard.agent_status.running_jobs }} 个进行中任务
      </span>
    </section>

    <section class="activity-demo" aria-labelledby="demo-heading">
      <div>
        <p class="eyebrow">答辩现场入口</p>
        <h2 id="demo-heading">随机校园通知测试</h2>
        <p>输入陌生通知后，材料会经过与真实采集相同的提取、候选、Agent 工具和审计链路；这里不返回预设答案。</p>
      </div>
      <form @submit.prevent="runDemo">
        <mdui-text-field
          label="随机测试通知正文"
          :value="demoText"
          rows="4"
          autosize
          max-rows="8"
          maxlength="10000"
          placeholder="例如：下周二下午的计算机组成原理实验调整到周四19:00，地点改为实验楼C308，请大家周三晚上前完成预习报告。"
          @input="demoText = readMduiValue($event)"
        ></mdui-text-field>
        <div class="activity-demo-actions">
          <mdui-button type="submit" variant="filled" :disabled="demoBusy || !demoText.trim()">
            {{ demoBusy ? '处理中…' : '提交真实处理' }}
          </mdui-button>
          <span>{{ demoState }}</span>
        </div>
      </form>
      <div v-if="demoResult" class="activity-demo-result">
        <strong>{{ demoResult.title }}</strong>
        <p>{{ demoResult.summary }}</p>
        <p v-if="demoAgentState" class="helper-text">{{ demoAgentState }}</p>
        <ol v-if="demoProgress.length" class="demo-progress-list">
          <li v-for="event in demoProgress" :key="event.sequence" :data-status="event.status">
            <span></span>
            <strong>{{ event.label }}</strong>
          </li>
        </ol>
      </div>
    </section>

    <div class="activity-columns">
      <section class="activity-panel" aria-labelledby="activity-timeline-heading">
        <div class="section-heading">
          <div>
            <p class="eyebrow">操作轨迹</p>
            <h2 id="activity-timeline-heading">最近变更</h2>
          </div>
          <SingleChoiceSegmented
            class="activity-filter"
            :model-value="actorFilter"
            aria-label="操作来源筛选"
            @update:model-value="actorFilter = $event as 'all' | 'agent' | 'user' | 'system'"
          >
            <mdui-segmented-button value="all">全部</mdui-segmented-button>
            <mdui-segmented-button value="agent">Agent</mdui-segmented-button>
            <mdui-segmented-button value="user">我</mdui-segmented-button>
          </SingleChoiceSegmented>
        </div>
        <div v-if="loading && !operations.length" class="activity-loading">
          <mdui-circular-progress></mdui-circular-progress>
        </div>
        <ol v-else-if="visibleOperations.length" class="activity-timeline">
          <li v-for="item in visibleOperations" :key="item.id">
            <span class="activity-dot" :data-actor="item.actor_type" aria-hidden="true"></span>
            <div class="activity-entry">
              <div>
                <strong>{{ actionLabel(item.action) }}</strong>
                <span class="activity-actor">{{ actorLabel(item.actor_type) }}</span>
                <time>{{ timestamp(item.created_at_epoch) }}</time>
              </div>
              <p>{{ item.summary }}</p>
              <div class="activity-entry-actions">
                <RouterLink class="app-link" :to="objectHref(item)">查看对象</RouterLink>
                <mdui-button
                  v-if="item.undoable && item.change_set_id"
                  variant="text"
                  :disabled="Boolean(busyId)"
                  @click="undo(item)"
                >
                  {{ busyId === item.id ? '撤销中…' : '撤销' }}
                </mdui-button>
              </div>
            </div>
          </li>
        </ol>
        <p v-else class="helper-text">还没有符合筛选条件的操作记录。</p>
      </section>

      <aside class="activity-side">
        <section class="activity-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">主动服务</p>
              <h2>接下来的提醒</h2>
            </div>
          </div>
          <ol v-if="upcomingReminders.length" class="activity-reminders">
            <li v-for="item in upcomingReminders" :key="item.id">
              <span>{{ timestamp(item.scheduled_at_epoch) }}</span>
              <strong>{{ item.metadata.title ?? '课程或任务提醒' }}</strong>
              <small>{{ item.status === 'queued' ? '正在合并投递' : `提前 ${Math.round((item.offset_minutes ?? 0) / 60)} 小时` }}</small>
            </li>
          </ol>
          <p v-else class="helper-text">暂无已安排的提醒；课程或任务有明确时间且启用提醒后会出现在这里。</p>
        </section>

        <section class="activity-panel">
          <div class="section-heading">
            <div>
              <p class="eyebrow">阶段总结</p>
              <h2>最新报告</h2>
            </div>
          </div>
          <template v-if="latestReport">
            <strong>{{ latestReport.subject }}</strong>
            <p class="helper-text">{{ reportStatus(latestReport.status) }} · {{ latestReport.period_start }}</p>
            <RouterLink
              class="activity-report-link"
              :to="{ path: '/settings/mail', query: { report: latestReport.id } }"
            >
              在邮件页查看详情 <AppIcon name="arrow_forward"></AppIcon>
            </RouterLink>
          </template>
          <p v-else class="helper-text">还没有日报或周报，可在邮件页手动生成。</p>
        </section>
      </aside>
    </div>
  </section>
</template>
