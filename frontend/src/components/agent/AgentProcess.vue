<script setup lang="ts">
import { computed } from 'vue';
import AppIcon from '../AppIcon.vue';

interface ToolTraceItem {
  action?: string;
  error?: boolean;
}

const props = defineProps<{
  metadata: Record<string, unknown>;
  streaming?: boolean;
  toolProgress?: string;
}>();

const THINKING_LABELS: Record<string, string> = {
  off: '关闭思考',
  low: '快速思考',
  medium: '标准思考',
  high: '深入思考',
  max: '最高强度',
};

const TOOL_LABELS: Record<string, string> = {
  get_agenda: '核对日程与空闲',
  organize_notification: '整理通知',
  manage_notification_mailbox: '整理通知分组',
  list_attachments: '查看附件',
  create_attachment: '生成附件',
  schedule_agent_task: '安排定时任务',
  list_agent_tasks: '查看定时任务',
  cancel_agent_task: '取消定时任务',
  get_user_settings: '查看设置',
  update_agent_settings: '修改助手设置',
  request_device_sync: '请求设备同步',
  search_notifications: '查找通知',
  search_items: '查找已有事项',
  read_attachment: '读取附件',
  propose_schedule: '准备日程变更',
  manage_personal_schedule: '维护个人日程',
  read_notification: '读取通知证据',
  decide_notification: '保存通知决定',
  apply_notification_changes: '执行变更',
  search_tasks: '检索事务',
  get_task: '读取事务',
  create_task: '创建事务',
  update_task: '修改事务',
  complete_task: '调整事务状态',
  search_courses: '检索课程',
  get_course: '读取课程',
  change_course_occurrence: '调整课程',
  create_course_makeup: '安排补课',
  link_entities: '建立关联',
  get_recent_changes: '读取最近变更',
  get_upcoming_deadlines: '检查临近截止',
  get_agent_insights: '读取值班建议',
  schedule_reminder: '安排提醒',
  undo_agent_action: '撤销操作',
  generate_report: '生成报告',
  search_history: '回查历史',
  search_knowledge: '检索知识',
  read_knowledge: '读取资料',
  search_web: '联网检索',
  read_web_page: '读取网页',
  save_knowledge: '保存资料',
  start_plan: '建立计划',
  update_plan_step: '更新计划',
  verify_citation: '校验引用',
  remember_memory: '保存长期记忆',
};

const tools = computed<ToolTraceItem[]>(() => (
  Array.isArray(props.metadata.tool_trace) ? props.metadata.tool_trace as ToolTraceItem[] : []
));
const reasoning = computed(() => String(props.metadata.reasoning ?? '').trim());
const thinking = computed(() => THINKING_LABELS[String(props.metadata.thinking_level ?? '')] ?? '标准思考');

function toolLabel(name?: string): string {
  return name ? TOOL_LABELS[name] ?? name : '工具操作';
}
</script>

<template>
  <!-- 历史回复没有可展示的过程时，不放一个只能展开空白说明的入口。 -->
  <details v-if="streaming || reasoning || tools.length" class="agent-process">
    <summary>
      <AppIcon name="tune"></AppIcon>
      <span>{{ streaming ? '正在处理' : '思考与工具' }}</span>
      <small v-if="!streaming">{{ tools.length ? `${tools.length} 个工具` : thinking }}</small>
      <span v-if="streaming" class="agent-process-live">{{ toolProgress || '正在思考…' }}</span>
      <AppIcon class="agent-process-chevron" name="chevron_right"></AppIcon>
    </summary>
    <div class="agent-process-body">
      <p v-if="reasoning" class="agent-process-reasoning">{{ reasoning }}</p>
      <p v-else class="agent-process-empty">{{ streaming ? '等待模型返回思考内容…' : '本轮模型未返回可展示的思考内容。' }}</p>
      <ul v-if="tools.length" class="agent-process-tools">
        <li v-for="(tool, index) in tools" :key="`${tool.action}-${index}`" :data-error="Boolean(tool.error)">
          <span>{{ index + 1 }}</span>
          {{ toolLabel(tool.action) }}
          <small>{{ tool.error ? '失败' : '完成' }}</small>
        </li>
      </ul>
      <p class="agent-process-meta">{{ thinking }}</p>
    </div>
  </details>
</template>
