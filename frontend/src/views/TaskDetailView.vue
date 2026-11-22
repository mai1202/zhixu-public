<script setup lang="ts">
import RequirementPanel from '../components/RequirementPanel.vue';
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { cancelTask, setTaskStatus, undoChange } from '../api/business';
import { ApiError } from '../api/client';
import { statusLabel } from '../api/labels';
import { getTaskDetail, patchTask, type TaskDetail } from '../api/ops';
import AppIcon from '../components/AppIcon.vue';
import { readMduiChecked, readMduiValue } from '../utils/mdui';

const route = useRoute();
const router = useRouter();
const detail = ref<TaskDetail | null>(null);
const loading = ref(false);
const saving = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
const title = ref('');
const description = ref('');
const taskKind = ref('assignment');
const priority = ref('normal');
const dueDate = ref('');
const dueTime = ref('');
const reminderEnabled = ref(true);
let disposed = false;
let requestVersion = 0;
let pageVersion = 0;

const linkedNotice = computed(() => {
  const linked = detail.value?.links.find((item) => item.from_type === 'notice' || item.to_type === 'notice');
  if (linked) return linked;
  const task = detail.value?.task;
  // 直接字段是业务事实；即使关系表记录缺失，也保留通知追溯入口。
  if (!task?.notice_id) return null;
  return {
    id: `direct-notice-${task.notice_id}`,
    from_type: 'notice',
    from_id: task.notice_id,
    to_type: 'task',
    to_id: task.id,
    relation: 'direct_source',
    from_title: task.notice_title ?? '来源通知',
    to_title: task.title,
    status: 'active',
  };
});
const linkedCourse = computed(() => {
  const linked = detail.value?.links.find(
    (item) => ['course_occurrence', 'course'].some((type) => item.from_type === type || item.to_type === type),
  );
  if (linked) return linked;
  const task = detail.value?.task;
  if (!task?.course_series_id && !task?.course_occurrence_id) return null;
  const occurrence = Boolean(task.course_occurrence_id);
  return {
    id: `direct-course-${task.course_occurrence_id ?? task.course_series_id}`,
    from_type: 'task',
    from_id: task.id,
    to_type: occurrence ? 'course_occurrence' : 'course',
    to_id: occurrence ? task.course_occurrence_id! : task.course_series_id!,
    relation: 'direct_context',
    from_title: task.title,
    to_title: task.course_title ?? '关联课程',
    status: 'active',
  };
});
const openTasksRemaining = computed(() => detail.value?.task.status === 'open');

function courseLinkTarget(): string | { path: string; query?: Record<string, string> } {
  const link = linkedCourse.value;
  if (!link) return '/courses';
  if (link.from_type === 'course_occurrence' || link.to_type === 'course_occurrence') {
    const date = detail.value?.task.course_date;
    return {
      path: '/schedule',
      query: {
        focus: link.from_type === 'course_occurrence' ? link.from_id : link.to_id,
        ...(date ? { date } : {}),
      },
    };
  }
  return '/courses';
}

onMounted(() => void refresh());
watch(() => route.params.id, () => {
  // Vue 会复用同一路由组件；换任务时清除旧表单，旧读取和旧保存均不能覆盖新任务。
  pageVersion += 1;
  requestVersion += 1;
  detail.value = null;
  saving.value = false;
  successMessage.value = '';
  void refresh();
});
onUnmounted(() => { disposed = true; pageVersion += 1; requestVersion += 1; });

async function refresh(): Promise<void> {
  if (disposed) return;
  const request = ++requestVersion;
  const id = String(route.params.id);
  loading.value = true;
  errorMessage.value = '';
  try {
    const result = await getTaskDetail(id);
    if (disposed || request !== requestVersion || id !== String(route.params.id)) return;
    detail.value = result;
    title.value = result.task.title;
    description.value = result.task.description ?? '';
    taskKind.value = result.task.task_kind;
    priority.value = result.task.priority;
    dueDate.value = result.task.due?.local_date ?? '';
    dueTime.value = result.task.due?.local_time?.slice(0, 5) ?? '';
    reminderEnabled.value = result.task.reminder_enabled;
  } catch (error) {
    if (!disposed && request === requestVersion) setError(error, '任务详情暂时无法读取。');
  } finally {
    if (!disposed && request === requestVersion) loading.value = false;
  }
}

async function mutate(taskId: string, action: () => Promise<unknown>, success: string, fallback: string): Promise<void> {
  if (disposed || saving.value || loading.value || detail.value?.task.id !== taskId || String(route.params.id) !== taskId) return;
  const page = pageVersion;
  saving.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    await action();
    if (disposed || page !== pageVersion) return;
    successMessage.value = success;
    await refresh();
  } catch (error) {
    if (!disposed && page === pageVersion) setError(error, fallback);
  } finally {
    if (!disposed && page === pageVersion) saving.value = false;
  }
}

async function save(): Promise<void> {
  if (!detail.value || !title.value.trim()) return;
  try {
    const task = detail.value.task;
    const zone = detail.value.task.due?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
    const deadline = dueDate.value
      ? {
          raw: dueTime.value ? `${dueDate.value} ${dueTime.value}` : dueDate.value,
          local_date: dueDate.value,
          local_time: dueTime.value ? `${dueTime.value}:00` : null,
          timezone: dueTime.value ? zone : null,
          precision: dueTime.value ? 'minute' : 'day',
          utc: dueTime.value ? zonedIso(dueDate.value, dueTime.value, zone) : null,
          basis: '用户在任务详情中确认。',
        }
      : null;
    await mutate(task.id, () => patchTask(task.id, {
      expected_version: task.version,
      title: title.value.trim(),
      description: description.value.trim() || null,
      task_kind: taskKind.value,
      priority: priority.value,
      deadline,
      reminder_enabled: reminderEnabled.value,
    }), '任务已保存，截止提醒会按新期限重新安排。', '任务没有保存，请刷新后重试。');
  } catch (error) {
    setError(error, '任务没有保存，请刷新后重试。');
  }
}

async function toggleStatus(): Promise<void> {
  if (!detail.value) return;
  const task = detail.value.task;
  const complete = task.status === 'open';
  await mutate(task.id, () => setTaskStatus(task, complete ? 'complete' : 'reopen'),
    complete ? '任务已完成。' : '任务已重新打开。', '任务状态没有更新。');
}

async function cancelCurrentTask(): Promise<void> {
  if (!detail.value || !window.confirm('取消这个任务？任务会保留为已取消，可随时重新打开。')) return;
  const task = detail.value.task;
  await mutate(task.id, () => cancelTask(task), '任务已取消，仍保留在历史记录中。', '任务没有取消。');
}

async function undo(changeSetId: string): Promise<void> {
  if (!detail.value || !window.confirm('撤销这次任务修改？')) return;
  await mutate(detail.value.task.id, () => undoChange(changeSetId), '这次修改已撤销。', '该修改已有后续变化，不能自动撤销。');
}

function zonedIso(date: string, clock: string, zoneName: string): string {
  const [year, month, day] = date.split('-').map(Number);
  const [hour, minute] = clock.split(':').map(Number);
  const naive = Date.UTC(year, month - 1, day, hour, minute);
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: zoneName,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const parts = Object.fromEntries(formatter.formatToParts(new Date(naive)).map((part) => [part.type, part.value]));
  const zonedAsUtc = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute));
  const offsetMinutes = Math.round((zonedAsUtc - naive) / 60_000);
  return new Date(naive - offsetMinutes * 60_000).toISOString();
}

function setError(error: unknown, fallback: string): void {
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
  successMessage.value = '';
}

function timestamp(epoch: number): string {
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(epoch * 1000));
}
</script>

<template>
  <section class="page-section task-detail-page" aria-labelledby="task-detail-heading">
    <header class="task-detail-header">
      <div>
        <p class="eyebrow">任务详情</p>
        <h1 id="task-detail-heading">{{ detail?.task.title ?? '任务' }}</h1>
        <p v-if="detail">
          {{ statusLabel(detail.task.status) }}
          <span v-if="detail.task.notice_title"> · 来源：{{ detail.task.notice_title }}</span>
        </p>
      </div>
      <div class="task-detail-actions">
        <mdui-button v-if="detail" variant="outlined" @click="router.push({ path: '/matters/journeys', query: { task: detail.task.id } })">办理路线与时间试算</mdui-button>
        <mdui-button variant="text" @click="router.push('/matters')">返回事务</mdui-button>
        <mdui-button v-if="detail?.task.status === 'open'" variant="text" :disabled="saving" @click="cancelCurrentTask">取消任务</mdui-button>
        <mdui-button v-if="detail" variant="outlined" :disabled="saving" @click="toggleStatus">
          {{ openTasksRemaining ? '标记完成' : '重新打开' }}
        </mdui-button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <div v-if="loading && !detail" class="task-detail-loading"><mdui-circular-progress></mdui-circular-progress></div>
    <div v-else-if="detail" class="task-detail-layout">
      <form class="task-detail-form" @submit.prevent="save">
        <mdui-text-field label="标题" :value="title" maxlength="500" required @input="title = readMduiValue($event)"></mdui-text-field>
        <mdui-text-field label="说明" :value="description" rows="5" autosize max-rows="10" maxlength="5000" placeholder="补充提交要求、材料或上下文" @input="description = readMduiValue($event)"></mdui-text-field>
        <div class="task-form-grid">
          <mdui-select variant="outlined" label="类型" :value="taskKind" @change="taskKind = readMduiValue($event)">
            <mdui-menu-item value="assignment">作业</mdui-menu-item>
            <mdui-menu-item value="exam">考试</mdui-menu-item>
            <mdui-menu-item value="preparation">预习/准备</mdui-menu-item>
            <mdui-menu-item value="activity">活动</mdui-menu-item>
            <mdui-menu-item value="course">课程事务</mdui-menu-item>
            <mdui-menu-item value="personal">个人事项</mdui-menu-item>
          </mdui-select>
          <mdui-select variant="outlined" label="优先级" :value="priority" @change="priority = readMduiValue($event)">
            <mdui-menu-item value="low">低</mdui-menu-item>
            <mdui-menu-item value="normal">普通</mdui-menu-item>
            <mdui-menu-item value="high">重要</mdui-menu-item>
            <mdui-menu-item value="urgent">紧急</mdui-menu-item>
          </mdui-select>
          <mdui-text-field label="截止日期" type="date" :value="dueDate" @input="dueDate = readMduiValue($event)"></mdui-text-field>
          <mdui-text-field label="截止时间" type="time" :value="dueTime" @input="dueTime = readMduiValue($event)"></mdui-text-field>
        </div>
        <mdui-checkbox class="task-reminder-toggle" :checked="reminderEnabled" @change="reminderEnabled = readMduiChecked($event)">
          按截止期限启用邮件提醒
        </mdui-checkbox>
        <p class="helper-text">只填写日期时系统保留日期精度，不会自动改成 23:59。修改期限后，旧提醒会取消并重新安排。</p>
        <mdui-button type="submit" variant="filled" :disabled="saving || !title.trim()">
          {{ saving ? '保存中…' : '保存任务' }}
        </mdui-button>
      </form>

      <aside class="task-detail-context">
        <section class="task-context-card">
          <p class="eyebrow">三端关联</p>
          <h2>依据与上下文</h2>
          <RouterLink
            v-if="linkedNotice"
            :to="{
              path: '/inbox',
              query: {
                panel: 'notices',
                notice: linkedNotice.from_type === 'notice' ? linkedNotice.from_id : linkedNotice.to_id,
              },
            }"
            class="task-context-row"
          >
            <span>通知</span>
            <strong>{{ linkedNotice.from_type === 'notice' ? linkedNotice.from_title : linkedNotice.to_title }}</strong>
            <AppIcon name="chevron_right"></AppIcon>
          </RouterLink>
          <RouterLink
            v-if="linkedCourse"
            :to="courseLinkTarget()"
            class="task-context-row"
          >
            <span>课程</span>
            <strong>{{ linkedCourse.from_type === 'course_occurrence' ? linkedCourse.from_title : linkedCourse.to_title }}</strong>
            <AppIcon name="chevron_right"></AppIcon>
          </RouterLink>
          <p v-if="!linkedNotice && !linkedCourse" class="helper-text">这个任务还没有关联通知或课程。</p>
          <p v-if="detail.task.original_requirement" class="task-original-requirement">
            原始期限依据：{{ detail.task.original_requirement }}
          </p>
        </section>

        <section class="task-context-card">
          <p class="eyebrow">可追溯</p>
          <RequirementPanel v-if="typeof detail.event?.id === 'string'" :event-id="String(detail.event.id)" />
          <h2>修改记录</h2>
          <ol v-if="detail.operations.length" class="task-operation-list">
            <li v-for="item in detail.operations" :key="item.id">
              <time>{{ timestamp(item.created_at_epoch) }}</time>
              <p>{{ item.summary }}</p>
              <mdui-button v-if="item.undoable && item.change_set_id" variant="text" :disabled="saving" @click="undo(item.change_set_id)">撤销</mdui-button>
            </li>
          </ol>
          <p v-else class="helper-text">暂无修改记录。</p>
        </section>
      </aside>
    </div>
  </section>
</template>
