<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { RouterLink, useRoute } from 'vue-router';
import { ApiError } from '../api/client';
import AppIcon from '../components/AppIcon.vue';
import TimeZoneSelect from '../components/TimeZoneSelect.vue';
import { readMduiChecked, readMduiValue } from '../utils/mdui';
import {
  generateReport,
  listReminders,
  listReports,
  sendReport,
  type ReminderItem,
  type ReportItem,
} from '../api/ops';
import {
  getDigestPreferences,
  getMailStatus,
  listMailOutbox,
  previewDigest,
  queueTestMail,
  retryMail,
  updateDigestPreferences,
  type DigestPreferences,
  type MailOutboxItem,
} from '../api/mail';

const route = useRoute();
const preferences = ref<DigestPreferences | null>(null);
const include = ref({
  courses: true,
  arrangements: true,
  deadlines: true,
  changes: true,
  tomorrow: true,
  agent_summary: true,
  suggestions: true,
});
const enabled = ref(false);
const timezone = ref(Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai');
const sendTime = ref('07:30');
const weeklyEnabled = ref(false);
const weeklySendDay = ref(7);
const weeklySendTime = ref('18:00');
const reminderEnabled = ref(true);
const reminderOffsets = ref('1440,120');
const quietEnabled = ref(false);
const quietStart = ref('22:00');
const quietEnd = ref('07:00');
const notificationFlags = ref({
  task_reminder: true,
  course_reminder: true,
  important_notice: true,
  course_change: true,
  conflict_warning: true,
});
const recipientAddressId = ref('');
const preview = ref<Awaited<ReturnType<typeof previewDigest>> | null>(null);
const outbox = ref<MailOutboxItem[]>([]);
const reports = ref<ReportItem[]>([]);
const reminders = ref<ReminderItem[]>([]);
const selectedReport = ref<ReportItem | null>(null);
const transportStatus = ref<{ channel: string; status: string; delivery_claim: string } | null>(null);
const loading = ref(false);
const saving = ref(false);
const busy = ref(false);
const reportBusy = ref('');
const errorMessage = ref('');
const successMessage = ref('');
let disposed = false;
let reportDeepLinkResolved = false;

onMounted(() => void refresh());
onUnmounted(() => { disposed = true; });

function setError(error: unknown, fallback: string): void {
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
  successMessage.value = '';
}

async function refresh(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    const [setting, mailStatus, sent] = await Promise.all([
      getDigestPreferences(), getMailStatus(), listMailOutbox(),
    ]);
    const [reportResult, reminderResult] = await Promise.all([listReports(20), listReminders(50)]);
    preferences.value = setting;
    enabled.value = setting.enabled;
    timezone.value = setting.timezone || timezone.value;
    sendTime.value = setting.send_time;
    weeklyEnabled.value = setting.weekly_enabled;
    weeklySendDay.value = setting.weekly_send_day;
    weeklySendTime.value = setting.weekly_send_time;
    reminderEnabled.value = setting.reminder_enabled;
    reminderOffsets.value = setting.reminder_offsets.join(',');
    quietEnabled.value = setting.quiet_hours.enabled;
    quietStart.value = setting.quiet_hours.start;
    quietEnd.value = setting.quiet_hours.end;
    notificationFlags.value = { ...notificationFlags.value, ...setting.notification_flags };
    // 默认选本人已验证主地址；须用户保存，且不自动启用日报。
    recipientAddressId.value = setting.recipient_address_id ?? setting.addresses.find((address) => address.is_primary)?.id ?? '';
    include.value = { ...setting.include };
    transportStatus.value = mailStatus;
    outbox.value = sent.items;
    reports.value = reportResult.items;
    reminders.value = reminderResult.items;
    if (!reportDeepLinkResolved && typeof route.query.report === 'string') {
      // 从工作台或报告深链进入时直接展开所选快照。
      selectedReport.value = reports.value.find((report) => report.id === route.query.report) ?? null;
    }
    reportDeepLinkResolved = true;
  } catch (error) {
    setError(error, '邮件设置暂时无法加载。');
  } finally {
    loading.value = false;
  }
}

async function save(): Promise<void> {
  if (!preferences.value) return;
  saving.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    preferences.value = await updateDigestPreferences({
      expected_version: preferences.value.version,
      enabled: enabled.value,
      timezone: timezone.value,
      send_time: sendTime.value,
      recipient_address_id: recipientAddressId.value || null,
      include: include.value,
      weekly_enabled: weeklyEnabled.value,
      weekly_send_day: weeklySendDay.value,
      weekly_send_time: weeklySendTime.value,
      reminder_enabled: reminderEnabled.value,
      reminder_offsets: reminderOffsets.value
        .split(',')
        .map((value) => Number(value.trim()))
        .filter((value) => Number.isInteger(value) && value >= 0),
      notification_flags: notificationFlags.value,
      quiet_hours: {
        enabled: quietEnabled.value,
        start: quietStart.value,
        end: quietEnd.value,
      },
      merge_window_minutes: preferences.value.merge_window_minutes,
    });
    successMessage.value = '主动服务设置已保存。';
    await refresh();
  } catch (error) {
    setError(error, '保存日报设置失败。');
  } finally {
    saving.value = false;
  }
}

async function refreshOutbox(): Promise<void> {
  outbox.value = (await listMailOutbox()).items;
}

async function makeReport(kind: 'daily' | 'weekly', send = false): Promise<void> {
  reportBusy.value = `${kind}:${send ? 'send' : 'preview'}`;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const generated = await generateReport({ kind, send });
    selectedReport.value = 'report' in generated ? generated.report : generated;
    successMessage.value = send ? '报告已生成并进入邮件队列。' : '报告已生成并保存。';
    await refresh();
  } catch (error) {
    setError(error, '报告生成失败。');
  } finally {
    reportBusy.value = '';
  }
}

async function deliverReport(report: ReportItem): Promise<void> {
  reportBusy.value = `report:${report.id}`;
  try {
    await sendReport(report.id);
    successMessage.value = '报告已进入邮件队列。';
    await refresh();
  } catch (error) {
    setError(error, '报告未能排队发送。');
  } finally {
    reportBusy.value = '';
  }
}

async function showPreview(): Promise<void> {
  busy.value = true;
  errorMessage.value = '';
  try {
    preview.value = await previewDigest();
  } catch (error) {
    setError(error, '日报预览生成失败。');
  } finally {
    busy.value = false;
  }
}

async function sendTest(): Promise<void> {
  const recipient = preferences.value?.addresses.find((address) => address.id === recipientAddressId.value)?.email
    ?? preferences.value?.addresses.find((address) => address.is_primary)?.email;
  if (!recipient || !window.confirm(`将向 ${recipient} 排队发送一封不含业务数据的合成测试邮件，继续吗？`)) return;
  busy.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const created = await queueTestMail();
    successMessage.value = `测试邮件已进入队列，当前状态：${statusLabel(created.status)}。`;
    await refreshOutbox();
    for (let attempt = 0; attempt < 20 && !disposed; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      await refreshOutbox();
      const current = outbox.value.find((item) => item.id === created.id);
      if (current && ['accepted', 'failed', 'unknown', 'cancelled'].includes(current.status)) {
        successMessage.value = current.status === 'accepted'
          ? '发信服务已接受测试邮件；此状态不表示收件箱已送达。'
          : `测试邮件状态：${statusLabel(current.status)}。`;
        break;
      }
    }
  } catch (error) {
    setError(error, '测试邮件未能排队。');
  } finally {
    busy.value = false;
  }
}

async function retry(item: MailOutboxItem): Promise<void> {
  busy.value = true;
  try {
    await retryMail(item.id);
    successMessage.value = '已创建一次明确重试。';
    await refreshOutbox();
  } catch (error) {
    setError(error, '邮件不能安全重试。');
  } finally {
    busy.value = false;
  }
}

function statusLabel(status: string): string {
  return ({ queued: '排队中', sending: '发送中', accepted: '服务已接受', failed: '发送失败', unknown: '结果未知', cancelled: '已取消' } as Record<string, string>)[status] ?? status;
}

function timestamp(epoch: number | null): string {
  if (!epoch) return '—';
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(epoch * 1000));
}

function weekdayLabel(value: number): string {
  return ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][value - 1] ?? '周日';
}

function reportStatus(value: string): string {
  return ({ generated: '已生成', queued: '待投递', sent: '服务已接受', failed: '发送失败' } as Record<string, string>)[value] ?? value;
}
</script>

<template>
  <section class="page-section mail-page" aria-labelledby="mail-heading">
    <header class="mail-header">
      <div>
        <RouterLink class="app-link" to="/settings">← 返回设置目录</RouterLink>
        <p class="eyebrow">设置 / 提醒</p>
        <h1 id="mail-heading">邮件提醒</h1>
        <p>把日报、周报、课程开始与截止提醒发送到已验证邮箱。</p>
      </div>
      <div class="mail-header-actions">
        <mdui-button variant="text" :disabled="loading || busy" @click="refresh">刷新</mdui-button>
        <mdui-button variant="outlined" :disabled="busy || loading" @click="showPreview">预览日报</mdui-button>
        <mdui-button variant="outlined" :disabled="busy || loading" @click="sendTest">发送测试</mdui-button>
      </div>
    </header>

    <div v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</div>
    <div v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</div>

    <section class="mail-hero" :data-enabled="enabled">
      <span class="mail-hero-icon"><AppIcon name="mail"></AppIcon></span>
      <div>
        <p class="eyebrow">{{ enabled ? '已开启' : '未开启' }}</p>
        <h2>{{ enabled ? `每天 ${sendTime} 发送日报` : '每日日报尚未开启' }}</h2>
        <p>
          收件地址：{{ preferences?.recipient ?? preferences?.addresses.find((address) => address.id === recipientAddressId)?.email ?? '尚未选择' }}
          <template v-if="preferences?.next_send_epoch"> · 下次发送 {{ timestamp(preferences.next_send_epoch) }}</template>
        </p>
        <p class="helper-text">
          周报：{{ weeklyEnabled ? `${weekdayLabel(weeklySendDay)} ${weeklySendTime} 自动生成` : '未开启' }}
          · 主动提醒：{{ reminderEnabled ? `提前 ${reminderOffsets} 分钟` : '未开启' }}
        </p>
      </div>
    </section>

    <details class="mail-settings-panel" open>
      <summary>
        <span><AppIcon name="settings"></AppIcon><strong>编辑发送设置</strong></span>
        <small>{{ enabled ? '修改时间、收件地址或内容范围' : '开启日报或调整发送计划' }}</small>
      </summary>
      <form class="mail-settings-form" @submit.prevent="save">
        <mdui-checkbox class="mail-enabled-control" :checked="enabled" @change="enabled = readMduiChecked($event)">启用每日邮件</mdui-checkbox>
        <div class="mail-form-grid">
          <mdui-select variant="outlined" label="收件地址" :value="recipientAddressId" :disabled="!preferences?.addresses.length" @change="recipientAddressId = readMduiValue($event)">
            <mdui-menu-item value="">请选择已验证地址</mdui-menu-item>
            <mdui-menu-item v-for="address in preferences?.addresses ?? []" :key="address.id" :value="address.id">
              {{ address.email }}{{ address.is_primary ? '（主地址）' : '' }}
            </mdui-menu-item>
          </mdui-select>
          <mdui-text-field label="每日时间" type="time" :value="sendTime" @input="sendTime = readMduiValue($event)"></mdui-text-field>
          <TimeZoneSelect v-model="timezone" />
        </div>
        <fieldset class="mail-option-list">
          <legend>周报</legend>
          <mdui-checkbox :checked="weeklyEnabled" @change="weeklyEnabled = readMduiChecked($event)">每周自动生成并发送</mdui-checkbox>
          <mdui-select variant="outlined" label="发送日" :value="String(weeklySendDay)" @change="weeklySendDay = Number(readMduiValue($event)) || 1">
            <mdui-menu-item v-for="day in 7" :key="day" :value="String(day)">{{ weekdayLabel(day) }}</mdui-menu-item>
          </mdui-select>
          <mdui-text-field label="发送时间" type="time" :value="weeklySendTime" @input="weeklySendTime = readMduiValue($event)"></mdui-text-field>
        </fieldset>
        <fieldset class="mail-option-list">
          <legend>任务、课程与变更提醒</legend>
          <mdui-checkbox :checked="reminderEnabled" @change="reminderEnabled = readMduiChecked($event)">启用主动提醒</mdui-checkbox>
          <mdui-text-field label="提前分钟（任务与课程共用，逗号分隔）" :value="reminderOffsets" placeholder="1440,120" @input="reminderOffsets = readMduiValue($event)"></mdui-text-field>
          <mdui-checkbox :checked="notificationFlags.important_notice" @change="notificationFlags.important_notice = readMduiChecked($event)">重要通知</mdui-checkbox>
          <mdui-checkbox :checked="notificationFlags.course_change" @change="notificationFlags.course_change = readMduiChecked($event)">课程调整</mdui-checkbox>
          <mdui-checkbox :checked="notificationFlags.task_reminder" @change="notificationFlags.task_reminder = readMduiChecked($event)">截止提醒</mdui-checkbox>
          <mdui-checkbox :checked="notificationFlags.course_reminder" @change="notificationFlags.course_reminder = readMduiChecked($event)">课程开始</mdui-checkbox>
          <mdui-checkbox :checked="notificationFlags.conflict_warning" @change="notificationFlags.conflict_warning = readMduiChecked($event)">冲突预警</mdui-checkbox>
        </fieldset>
        <fieldset class="mail-option-list">
          <legend>安静时段</legend>
          <mdui-checkbox :checked="quietEnabled" @change="quietEnabled = readMduiChecked($event)">非紧急提醒不在此期间发送</mdui-checkbox>
          <mdui-text-field label="开始" type="time" :value="quietStart" @input="quietStart = readMduiValue($event)"></mdui-text-field>
          <mdui-text-field label="结束" type="time" :value="quietEnd" @input="quietEnd = readMduiValue($event)"></mdui-text-field>
        </fieldset>
        <fieldset class="mail-option-list">
          <legend>日报包含</legend>
          <mdui-checkbox :checked="include.courses" @change="include.courses = readMduiChecked($event)">今日课程</mdui-checkbox>
          <mdui-checkbox :checked="include.arrangements" @change="include.arrangements = readMduiChecked($event)">已确认安排</mdui-checkbox>
          <mdui-checkbox :checked="include.deadlines" @change="include.deadlines = readMduiChecked($event)">未来 7 天截止</mdui-checkbox>
          <mdui-checkbox :checked="include.changes" @change="include.changes = readMduiChecked($event)">上次日报后的变更</mdui-checkbox>
          <mdui-checkbox :checked="include.tomorrow" @change="include.tomorrow = readMduiChecked($event)">明日预警</mdui-checkbox>
          <mdui-checkbox :checked="include.agent_summary" @change="include.agent_summary = readMduiChecked($event)">Agent 处理摘要</mdui-checkbox>
          <mdui-checkbox :checked="include.suggestions" @change="include.suggestions = readMduiChecked($event)">行动建议</mdui-checkbox>
        </fieldset>
        <p class="helper-text">收件地址和发送时间保存后生效；邮件预览使用已保存设置。日报默认关闭。</p>
        <div class="mail-action-row">
          <mdui-button variant="filled" type="submit" :disabled="saving || loading">{{ saving ? '保存中…' : '保存设置' }}</mdui-button>
        </div>
      </form>
    </details>

    <section class="mail-transport">
      <span class="mail-transport-dot" :data-ready="transportStatus?.status !== 'disabled'"></span>
      <div>
        <strong>运营发信通道</strong>
        <p v-if="transportStatus">{{ transportStatus.status === 'disabled' ? '尚未配置，请联系运营者。' : '已配置，可生成预览或发送合成测试邮件。' }}</p>
        <p>“服务已接受”只表示发信服务收到请求，不代表已经进入收件箱。</p>
      </div>
      <details v-if="transportStatus"><summary>诊断</summary><span>{{ transportStatus.channel }}</span></details>
    </section>

    <section v-if="preview" class="mail-preview-section" aria-labelledby="mail-preview-heading">
      <div class="section-heading">
        <div>
          <p class="eyebrow">预览</p>
          <h2 id="mail-preview-heading">{{ preview.subject }}</h2>
        </div>
        <span>{{ preview.local_date }}</span>
      </div>
      <p class="helper-text">收件人：{{ preview.recipient ?? '尚未选择已验证地址' }} · 变更序号 {{ preview.change_seq_from }} 至 {{ preview.change_seq_through }}</p>
      <pre>{{ preview.body_text }}</pre>
    </section>

    <section class="mail-outbox-section" aria-labelledby="report-heading">
      <div class="section-heading">
        <div>
          <p class="eyebrow">可回看的阶段总结</p>
          <h2 id="report-heading">日报与周报</h2>
        </div>
        <span>{{ reports.length }} 份</span>
      </div>
      <div class="mail-action-row">
        <mdui-button variant="outlined" :disabled="Boolean(reportBusy)" @click="makeReport('daily')">
          {{ reportBusy === 'daily:preview' ? '生成中…' : '生成今日日报' }}
        </mdui-button>
        <mdui-button variant="outlined" :disabled="Boolean(reportBusy)" @click="makeReport('weekly')">
          {{ reportBusy === 'weekly:preview' ? '生成中…' : '生成本周周报' }}
        </mdui-button>
        <mdui-button variant="filled" :disabled="Boolean(reportBusy)" @click="makeReport('daily', true)">
          生成并发送日报
        </mdui-button>
      </div>
      <article v-for="report in reports" :key="report.id" class="mail-outbox-item">
        <div>
          <mdui-button variant="text" class="report-title-button" @click="selectedReport = report">{{ report.subject }}</mdui-button>
          <span>{{ report.period_start }} · {{ reportStatus(report.status) }}</span>
        </div>
        <div class="mail-outbox-status">
          <mdui-button v-if="report.status === 'generated' && preferences?.recipient" variant="text" :disabled="Boolean(reportBusy)" @click="deliverReport(report)">
            发送
          </mdui-button>
        </div>
      </article>
      <p v-if="!reports.length" class="helper-text">还没有报告。可以先生成日报核对事实，再选择发送。</p>
      <pre v-if="selectedReport" class="report-body">{{ selectedReport.body_text }}</pre>
      <div v-if="selectedReport?.links?.length" class="report-links">
        <span>关联对象</span>
        <RouterLink v-for="link in selectedReport.links ?? []" :key="link.type + link.id" class="app-link" :to="link.href">
          {{ link.type === 'notice' ? '通知' : link.type === 'task' ? '任务' : '课程' }} · {{ link.title }}
        </RouterLink>
      </div>
    </section>

    <section class="mail-outbox-section" aria-labelledby="reminder-heading">
      <div class="section-heading">
        <div>
          <p class="eyebrow">主动提醒计划</p>
          <h2 id="reminder-heading">接下来</h2>
        </div>
        <span>{{ reminders.filter((item) => ['scheduled', 'queued'].includes(item.status)).length }} 条</span>
      </div>
          <p v-if="!reminders.length" class="helper-text">课程或任务有明确时间后，系统会在这里显示提醒计划。</p>
      <article v-for="item in reminders.filter((entry) => ['scheduled', 'queued'].includes(entry.status)).slice(0, 8)" :key="item.id" class="mail-outbox-item">
        <div>
          <strong>{{ item.metadata.title ?? '课程或任务提醒' }}</strong>
          <span>{{ timestamp(item.scheduled_at_epoch) }} · {{ item.status === 'queued' ? '等待合并发送' : '已安排' }}</span>
        </div>
        <small v-if="item.offset_minutes !== null">提前 {{ item.offset_minutes }} 分钟</small>
      </article>
    </section>

    <section class="mail-outbox-section" aria-labelledby="mail-outbox-heading">
      <div class="section-heading">
        <div>
          <p class="eyebrow">投递记录</p>
          <h2 id="mail-outbox-heading">最近邮件</h2>
        </div>
        <span>{{ outbox.length }} 条</span>
      </div>
      <p v-if="!outbox.length" class="helper-text">目前没有排队或发送记录。</p>
      <article v-for="item in outbox" :key="item.id" class="mail-outbox-item">
        <div>
          <strong>{{ item.subject }}</strong>
          <span>{{ item.recipient }} · {{ timestamp(item.created_at_epoch) }}</span>
        </div>
        <div class="mail-outbox-status">
          <span>{{ statusLabel(item.status) }}</span>
          <mdui-button v-if="item.status === 'failed'" variant="text" :disabled="busy" @click="retry(item)">明确重试</mdui-button>
        </div>
        <small v-if="item.error_code">错误分类：{{ item.error_code }}</small>
      </article>
    </section>
  </section>
</template>
