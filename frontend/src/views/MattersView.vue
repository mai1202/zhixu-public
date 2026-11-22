<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { listTasks, setTaskStatus, type TaskSummary } from '../api/business';
import { ApiError } from '../api/client';
import { createManagedTask } from '../api/ops';
import { listJourneys, listOpportunities } from '../api/journeys';
import { listSchedule, type ScheduleItem } from '../api/timetable';
import { readMduiValue } from '../utils/mdui';
import AgentInsightPanel from '../components/AgentInsightPanel.vue';
import AgentRuntimeAlert from '../components/AgentRuntimeAlert.vue';
import AppIcon from '../components/AppIcon.vue';
import SingleChoiceSegmented from '../components/SingleChoiceSegmented.vue';

type MatterStatus = 'open' | 'completed' | 'cancelled';

const router = useRouter();
const today = localDate(new Date());
const status = ref<MatterStatus>('open');
const tasks = ref<TaskSummary[]>([]);
const openTasks = ref<TaskSummary[]>([]);
const arrangements = ref<ScheduleItem[]>([]);
const openTaskCount = ref(0);
const completedTaskCount = ref(0);
const loading = ref(false);
const busyId = ref('');
const errorMessage = ref('');
const successMessage = ref('');
const searchQuery = ref('');
const showEditor = ref(false);
const newTitle = ref('');
const newDate = ref(today);
const newPriority = ref('normal');
const newKind = ref('assignment');
const creating = ref(false);
const hubJourneyCount = ref<number | null>(null);
const hubOpportunityCount = ref<number | null>(null);

const visibleTasks = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase();
  return tasks.value
    .filter((task) => !query || [task.title, task.notice_title, task.course_title, task.priority].filter(Boolean).join(' ').toLocaleLowerCase().includes(query))
    .sort((left, right) => priorityValue(right) - priorityValue(left) || dueValue(left) - dueValue(right));
});
const overdueCount = computed(() => openTasks.value.filter((task) => Boolean(task.due?.local_date && task.due.local_date < today)).length);
const dueSoonCount = computed(() => openTasks.value.filter((task) => {
  const due = task.due?.local_date;
  return Boolean(due && due >= today && due <= addDays(today, 2));
}).length);

onMounted(() => {
  void refresh();
  void loadHub();
});

async function loadHub(): Promise<void> {
  // 入口卡的数字是增强信息，失败不影响事务列表本身。
  const [journeys, opportunities] = await Promise.allSettled([listJourneys(), listOpportunities()]);
  if (journeys.status === 'fulfilled') hubJourneyCount.value = journeys.value.total;
  if (opportunities.status === 'fulfilled') hubOpportunityCount.value = opportunities.value.items.length;
}

async function refresh(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    const [taskResult, openResult, completedResult, scheduleResult] = await Promise.all([
      listTasks(status.value),
      status.value === 'open' ? Promise.resolve(null) : listTasks('open'),
      listTasks('completed'),
      listSchedule(today, addDays(today, 6)),
    ]);
    tasks.value = taskResult.items;
    openTasks.value = status.value === 'open' ? taskResult.items : (openResult?.items ?? []);
    openTaskCount.value = openTasks.value.length;
    completedTaskCount.value = completedResult.items.length;
    arrangements.value = scheduleResult.items.filter((item) => item.item_type === 'personal');
  } catch (error) {
    setError(error, '事务暂时无法加载。');
  } finally {
    loading.value = false;
  }
}

async function createTask(): Promise<void> {
  if (!newTitle.value.trim()) return;
  creating.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    await createManagedTask({
      title: newTitle.value.trim(),
      task_kind: newKind.value,
      priority: newPriority.value,
      deadline: newDate.value ? {
        raw: newDate.value,
        local_date: newDate.value,
        local_time: null,
        timezone: null,
        precision: 'day',
        utc: null,
        basis: '用户在事务中心填写。',
      } : null,
      reminder_enabled: true,
    });
    newTitle.value = '';
    newDate.value = today;
    showEditor.value = false;
    successMessage.value = '事务已创建；如有明确期限，Agent 会按设置安排提醒。';
    status.value = 'open';
    await refresh();
  } catch (error) {
    setError(error, '事务创建失败。');
  } finally {
    creating.value = false;
  }
}

async function toggleTask(task: TaskSummary): Promise<void> {
  if (busyId.value) return;
  busyId.value = task.id;
  try {
    await setTaskStatus(task, task.status === 'open' ? 'complete' : 'reopen');
    successMessage.value = task.status === 'open' ? '事务已完成。' : '事务已重新打开。';
    await refresh();
  } catch (error) {
    setError(error, '事务状态没有更新。');
  } finally {
    busyId.value = '';
  }
}

function setError(error: unknown, fallback: string): void {
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
  successMessage.value = '';
}

function localDate(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

function addDays(value: string, days: number): string {
  const date = new Date(`${value}T12:00:00`);
  date.setDate(date.getDate() + days);
  return localDate(date);
}

function priorityValue(task: TaskSummary): number {
  return ({ urgent: 4, high: 3, normal: 2, low: 1 } as Record<string, number>)[task.priority] ?? 0;
}

function dueValue(task: TaskSummary): number {
  return task.due?.local_date ? Date.parse(`${task.due.local_date}T${task.due.local_time ?? '23:59'}:00`) : Number.MAX_SAFE_INTEGER;
}

function priorityLabel(value: string): string {
  return ({ urgent: '紧急', high: '重要', normal: '普通', low: '低' } as Record<string, string>)[value] ?? value;
}

function kindLabel(value: string): string {
  return ({ assignment: '作业', exam: '考试', preparation: '预习/准备', activity: '活动', course: '课程事务', personal: '个人' } as Record<string, string>)[value] ?? value;
}

function dueLabel(task: TaskSummary): string {
  const date = task.due?.local_date;
  if (!date) return '未设置期限';
  if (date < today) return `已逾期 · ${date.slice(5)}`;
  if (date === today) return task.due?.local_time ? `今天 ${task.due.local_time.slice(0, 5)}` : '今天截止';
  if (date === addDays(today, 1)) return '明天截止';
  return `${date.slice(5)} 截止`;
}

function itemTime(item: ScheduleItem): string {
  return item.start_at ? `${item.start_at.slice(11, 16)}${item.end_at ? `–${item.end_at.slice(11, 16)}` : ''}` : '全天';
}
</script>

<template>
  <section class="page-section matters-page" aria-labelledby="matters-heading">
    <header class="matters-header">
      <h1 id="matters-heading">事务</h1>
      <div class="matters-header-actions">
        <mdui-button variant="filled" @click="showEditor = !showEditor">
          <AppIcon name="add" slot="icon"></AppIcon>
          新建事务
        </mdui-button>
      </div>
    </header>

    <section class="matters-metrics" aria-label="事务概况">
      <article><span>待处理</span><strong>{{ openTaskCount }}</strong></article>
      <article><span>已逾期</span><strong>{{ overdueCount }}</strong></article>
      <article><span>三日内截止</span><strong>{{ dueSoonCount }}</strong></article>
      <article><span>已完成</span><strong>{{ completedTaskCount }}</strong></article>
    </section>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <form v-if="showEditor" class="matter-editor" @submit.prevent="createTask">
      <mdui-text-field
        label="事项标题"
        :value="newTitle"
        maxlength="500"
        required
        placeholder="例如：完成高数第三章习题"
        @input="newTitle = readMduiValue($event)"
      ></mdui-text-field>
      <div class="matter-editor-grid">
        <mdui-select variant="outlined" label="类型" :value="newKind" @change="newKind = readMduiValue($event)">
          <mdui-menu-item value="assignment">作业</mdui-menu-item>
          <mdui-menu-item value="exam">考试</mdui-menu-item>
          <mdui-menu-item value="preparation">预习/准备</mdui-menu-item>
          <mdui-menu-item value="activity">活动</mdui-menu-item>
          <mdui-menu-item value="course">课程事务</mdui-menu-item>
          <mdui-menu-item value="personal">个人事务</mdui-menu-item>
        </mdui-select>
        <mdui-select variant="outlined" label="优先级" :value="newPriority" @change="newPriority = readMduiValue($event)">
          <mdui-menu-item value="low">低</mdui-menu-item>
          <mdui-menu-item value="normal">普通</mdui-menu-item>
          <mdui-menu-item value="high">重要</mdui-menu-item>
          <mdui-menu-item value="urgent">紧急</mdui-menu-item>
        </mdui-select>
        <mdui-text-field label="截止日期" type="date" :value="newDate" @input="newDate = readMduiValue($event)"></mdui-text-field>
      </div>
      <div class="matter-editor-actions">
        <mdui-button type="submit" variant="filled" :disabled="creating || !newTitle.trim()">{{ creating ? '创建中…' : '创建事务' }}</mdui-button>
        <mdui-button type="button" variant="text" @click="showEditor = false">取消</mdui-button>
      </div>
    </form>

    <div class="matters-columns">
      <section class="matters-panel" aria-label="事务清单">
        <div class="section-heading matters-list-heading">
          <SingleChoiceSegmented
            class="matter-status-switch"
            :model-value="status"
            aria-label="事务状态"
            @update:model-value="status = $event as MatterStatus; refresh()"
          >
            <mdui-segmented-button value="open">待处理</mdui-segmented-button>
            <mdui-segmented-button value="completed">已完成</mdui-segmented-button>
            <mdui-segmented-button value="cancelled">已取消</mdui-segmented-button>
          </SingleChoiceSegmented>
        </div>
        <mdui-text-field
          class="matter-search"
          type="search"
          variant="outlined"
          clearable
          :value="searchQuery"
          placeholder="搜索事务、通知来源或课程"
          aria-label="搜索事务"
          @input="searchQuery = readMduiValue($event)"
        >
          <AppIcon slot="icon" name="search"></AppIcon>
        </mdui-text-field>
        <div v-if="loading && !tasks.length" class="matter-loading"><mdui-circular-progress></mdui-circular-progress></div>
        <div v-else class="matter-list">
          <article v-for="task in visibleTasks" :key="task.id" class="matter-row" :data-status="task.status" :data-priority="task.priority">
            <!-- 完成操作放在行首，任务正文保留详情链接。 -->
            <mdui-button-icon
              class="matter-toggle"
              :aria-label="`${task.status === 'open' ? '完成' : '重开'}：${task.title}`"
              :loading="busyId === task.id"
              :disabled="Boolean(busyId)"
              @click="toggleTask(task)"
            >
              <AppIcon :name="task.status === 'open' ? 'radio_button_unchecked' : 'check_circle'" />
            </mdui-button-icon>
            <div class="matter-row-main">
              <div class="matter-row-title">
                <RouterLink :to="`/tasks/${task.id}`">{{ task.title }}</RouterLink>
                <span>{{ kindLabel(task.task_kind) }}</span>
              </div>
              <p>
                <span v-if="task.priority !== 'normal'" class="matter-priority">{{ priorityLabel(task.priority) }}</span>
                <span :data-overdue="Boolean(task.status === 'open' && task.due?.local_date && task.due.local_date < today)">{{ dueLabel(task) }}</span>
                <template v-if="task.course_title"> · {{ task.course_title }}</template>
                <template v-if="task.notice_title"> · 来源：{{ task.notice_title }}</template>
              </p>
            </div>
          </article>
          <p v-if="!loading && !visibleTasks.length" class="matter-empty">
            {{ searchQuery ? '没有匹配的事务。' : status === 'open' ? '没有待处理事务，Agent 仍会在后台检查新变化。' : '当前筛选下没有记录。' }}
          </p>
        </div>
      </section>

      <aside class="matters-side">
        <mdui-card class="matters-hub" variant="filled" aria-label="办理与发现">
          <div class="matters-hub-heading">
            <div>
              <p class="eyebrow">从通知到完成</p>
              <h2>办理与发现</h2>
            </div>
            <span>按当前事务需要进入</span>
          </div>
          <mdui-list>
            <mdui-list-item href="/matters/planning" @click.prevent="router.push('/matters/planning')">统一安排事务<div slot="description">多个事务共用容量，明确授权后局部重排。</div></mdui-list-item>
            <mdui-list-item
              href="/matters/journeys"
              @click.prevent="router.push('/matters/journeys')"
            >
              <AppIcon slot="icon" name="route"></AppIcon>
              <div slot="description">理清材料、步骤与截止前的时间安排。</div>
              <span slot="end-icon" class="matters-hub-count">{{ hubJourneyCount === null ? '—' : `${hubJourneyCount} 条` }}</span>
              办理路线
            </mdui-list-item>
            <mdui-list-item
              href="/matters/opportunities"
              @click.prevent="router.push('/matters/opportunities')"
            >
              <AppIcon slot="icon" name="explore"></AppIcon>
              <div slot="description">按兴趣、条件和空闲窗口匹配通知机会。</div>
              <span slot="end-icon" class="matters-hub-count">{{ hubOpportunityCount === null ? '—' : `${hubOpportunityCount} 个` }}</span>
              发现机会
            </mdui-list-item>
          </mdui-list>
        </mdui-card>

        <AgentRuntimeAlert compact />

        <AgentInsightPanel
          module="task"
          collapsible
          empty-text="Agent 已检查近期任务，没有发现逾期、冲突或缺失期限的事务。"
        />
        <section class="matters-panel">
          <div class="section-heading">
            <div><p class="eyebrow">未来 7 天</p><h2>个人时间块</h2></div>
            <RouterLink to="/schedule">管理</RouterLink>
          </div>
          <ol v-if="arrangements.length" class="matter-arrangements">
            <li v-for="item in arrangements.slice(0, 8)" :key="item.id">
              <time>{{ item.local_date.slice(5) }} · {{ itemTime(item) }}</time>
              <strong>{{ item.title }}</strong>
            </li>
          </ol>
          <p v-else class="matter-empty">未来 7 天没有个人安排；课表和完整日程可在“日程任务”查看。</p>
        </section>
      </aside>
    </div>
  </section>
</template>
