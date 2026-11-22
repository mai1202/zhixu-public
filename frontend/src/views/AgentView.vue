<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import { statusLabel } from '../api/labels';
import {
  AGENT_THINKING_OPTIONS,
  archiveAgentConversation,
  cancelAgentRun,
  getAgentConversation,
  getAgentRun,
  listAgentConversations,
  submitAgentTurn,
  uploadAgentAttachment,
  getAgentAttachment,
  getAgentPreferences,
  restoreMainConversation,
  restoreAgentConversation,
  streamAgentEvents,
  type AgentAttachment,
  type AgentConversationSummary,
  type AgentThinkingLevel,
  type AgentTurn,
} from '../api/agent';
import { confirmChange, rejectChange, undoChange, type ChangeSetSummary } from '../api/business';
import { listProviderProfiles, type ProviderProfile } from '../api/models';
import { useAuthStore } from '../stores/auth';
import {
  clearConversationCache,
  deleteCachedConversation,
  getCachedConversation,
  latestSequence,
  mergeTurns,
  putCachedConversation,
  type CachedAgentConversation,
} from '../stores/agent-cache';
import { copyText } from '../utils/clipboard';
import { formatConversationDate, formatRelativeDateTime } from '../utils/datetime';
import AppIcon from '../components/AppIcon.vue';
import AgentAutoCard from '../components/agent/AgentAutoCard.vue';
import AgentComposer from '../components/agent/AgentComposer.vue';
import AgentMarkdown from '../components/agent/AgentMarkdown.vue';
import AgentProcess from '../components/agent/AgentProcess.vue';
import AgentThinkingDots from '../components/agent/AgentThinkingDots.vue';

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();
const userId = computed(() => auth.user?.id ?? '');

const conversations = ref<AgentConversationSummary[]>([]);
const archivedConversations = ref<AgentConversationSummary[]>([]);
const currentConversation = ref<AgentConversationSummary | null>(null);
const turns = ref<AgentTurn[]>([]);
const turnsViewport = ref<HTMLElement | null>(null);
const composer = ref<InstanceType<typeof AgentComposer> | null>(null);
const profiles = ref<ProviderProfile[]>([]);
const selectedProfileId = ref('');
const thinkingLevel = ref<AgentThinkingLevel>('medium');
const message = ref('');
const attachments = ref<AgentAttachment[]>([]);
const draggingFiles = ref(false);
const uploading = ref(false);
const sending = ref(false);
const loading = ref(false);
const syncing = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
const showArchived = ref(false);
const conversationActionId = ref('');
const activeRunId = ref('');
const failedRunId = ref('');
const streamingRunId = ref('');
const streamingStartedAt = ref(Date.now());
const streamingText = ref('');
// 仅展示模型实际返回的 reasoning_content，不生成或补写思考内容。
const streamingReasoning = ref('');
const toolProgress = ref('');
const copiedTurnId = ref('');
const stickToBottom = ref(true);
const mobileQuery = (window.matchMedia
  // 与应用底栏及工作区样式共用 900px 断点，避免窄窗口落入桌面布局。
  ? window.matchMedia('(max-width: 900px)')
  : { matches: false, addEventListener: () => {}, removeEventListener: () => {} }) as MediaQueryList;
const mobileLayout = ref(mobileQuery.matches);
const historyOpen = ref(false);
const profileStorageKey = computed(() => `agent-chat-profile:${userId.value}`);
const thinkingStorageKey = computed(() => `agent-chat-thinking:${userId.value}`);
const draftKey = `assistant-draft:${userId.value}`;
const seedPromptKey = `agent-seeded-prompt:${userId.value}`;
const sidebarPreferenceKey = computed(() => `agent-sidebar-collapsed:${userId.value}`);

function readPreference(key: string): string {
  try { return typeof localStorage === 'undefined' ? '' : localStorage.getItem(key) ?? ''; } catch { return ''; }
}
function writePreference(key: string, value: string): void {
  try { if (typeof localStorage !== 'undefined') localStorage.setItem(key, value); } catch { /* 私密模式不阻止使用。 */ }
}
const sidebarCollapsed = ref(readPreference(sidebarPreferenceKey.value) !== '0');

try {
  const draft = JSON.parse(sessionStorage.getItem(draftKey) ?? '{}');
  message.value = typeof draft.message === 'string' ? draft.message : '';
  attachments.value = Array.isArray(draft.attachments) ? draft.attachments : [];
} catch { /* 存储被禁用时继续保留本页草稿。 */ }
watch([message, attachments], () => {
  try { sessionStorage.setItem(draftKey, JSON.stringify({ message: message.value, attachments: attachments.value })); }
  catch { /* 私密浏览模式不阻止发送。 */ }
}, { deep: true });

let disposed = false;
let conversationRequest = 0;
let initializationRequest = 0;
let refreshTimer: number | undefined;
let scrollFrame = 0;
let resizeObserver: ResizeObserver | undefined;
let runStream: AbortController | undefined;

const planningProfiles = computed(() => profiles.value.filter((profile) => profile.purpose === 'planning' && profile.enabled));
const displayedConversations = computed(() => showArchived.value ? archivedConversations.value : conversations.value);
const userRunIds = computed(() => new Set(turns.value.filter((turn) => turn.role === 'user' && turn.run_id).map((turn) => turn.run_id as string)));
const sidebarOpen = computed(() => mobileLayout.value ? historyOpen.value : !sidebarCollapsed.value);
const showScrollLatest = computed(() => turns.value.length > 0 && !stickToBottom.value && !loading.value);
const streamingTurn = computed<AgentTurn | null>(() => {
  if (!sending.value || !streamingRunId.value) return null;
  if (turns.value.some((turn) => turn.role === 'assistant' && turn.run_id === streamingRunId.value)) return null;
  return {
    id: `streaming-${streamingRunId.value}`,
    run_id: streamingRunId.value,
    sequence: latestSequence(turns.value) + 1,
    role: 'assistant',
    content: streamingText.value,
    metadata: { thinking_level: thinkingLevel.value, reasoning: streamingReasoning.value },
    created_at_epoch: Math.floor(streamingStartedAt.value / 1000),
  };
});
const displayedTurns = computed(() => streamingTurn.value ? [...turns.value, streamingTurn.value] : turns.value);
const canSend = computed(() => (
  currentConversation.value?.status !== 'archived'
  && Boolean(selectedProfileId.value)
  && Boolean(message.value.trim() || attachments.value.length)
  && !sending.value
  && !uploading.value
  && attachments.value.every((item) => item.status === 'ready')
));
const starterPrompts = [
  { label: '全面巡检校务', prompt: '请读取后台主动扫描建议和近期截止，检查我的课程、事务和通知，按紧急程度给出 5 项以内的行动建议；发现问题时说明是否已经写入以及能否撤销。' },
  { label: '看明天安排', prompt: '请查看我明天的课程、待办和通知，指出时间冲突，并给出具体的时间安排建议。' },
  { label: '找本周空闲', prompt: '请查找我本周的空闲时间，并说明适合安排复习或处理待办的时段。' },
  { label: '排今日优先级', prompt: '请根据今天的课程、待办和通知，帮我排出优先级并给出可执行的行动计划。' },
];

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
  preview_timetable_attachment: '预览课表',
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
const RUN_ERROR_LABELS: Record<string, string> = {
  platform_token_quota_exceeded: '今日模型预算已用完或已预留，请查看计费明细并等待北京时间零点重置',
  platform_pricing_unavailable: '平台模型单价暂不可核验，请到设置 → 模型连接绑定自己的模型',
  provider_request_failed: '模型请求失败',
  planning_profile_required: '尚未选择可用的规划模型',
  model_unavailable: '模型配置不可用',
  pi_unavailable: '助手服务暂时不可用',
  pi_run_failed: '助手运行失败',
  pi_stream_interrupted: '连接中断，后台会话已保留',
  invalid_tool_arguments: '业务工具参数未通过校验',
  run_not_retryable: '当前运行不能重试',
};

function isAgentPage(): boolean {
  return !disposed && route.path === '/agent';
}

onMounted(() => {
  void initialize();
  mobileQuery.addEventListener?.('change', syncMobileLayout);
  refreshTimer = window.setInterval(() => {
    if (!isAgentPage() || sending.value || loading.value || !currentConversation.value) return;
    if (document.visibilityState === 'hidden') return;
    void syncConversation(currentConversation.value.id);
  }, 5000);
  void nextTick(setupResizeObserver);
});

onUnmounted(() => {
  disposed = true;
  conversationRequest += 1;
  initializationRequest += 1;
  window.clearInterval(refreshTimer);
  window.cancelAnimationFrame(scrollFrame);
  mobileQuery.removeEventListener?.('change', syncMobileLayout);
  resizeObserver?.disconnect();
  runStream?.abort();
});

function setupResizeObserver(): void {
  if (typeof ResizeObserver === 'undefined' || !turnsViewport.value) return;
  resizeObserver = new ResizeObserver(() => {
    if (stickToBottom.value) jumpToBottom();
  });
  resizeObserver.observe(turnsViewport.value);
}

function syncMobileLayout(event: MediaQueryListEvent): void {
  mobileLayout.value = event.matches;
  if (!event.matches) historyOpen.value = false;
}

function toggleSidebar(): void {
  if (mobileLayout.value) {
    historyOpen.value = !historyOpen.value;
    return;
  }
  sidebarCollapsed.value = !sidebarCollapsed.value;
  writePreference(sidebarPreferenceKey.value, sidebarCollapsed.value ? '1' : '0');
  if (sidebarCollapsed.value) historyOpen.value = false;
}

function closeSidebar(): void {
  if (mobileLayout.value) historyOpen.value = false;
  else sidebarCollapsed.value = true;
}

async function initialize(): Promise<void> {
  if (!isAgentPage()) return;
  const request = ++initializationRequest;
  const isCurrent = () => isAgentPage() && request === initializationRequest;
  loading.value = true;
  errorMessage.value = '';
  const seededPrompt = sessionStorage.getItem(seedPromptKey) ?? '';
  sessionStorage.removeItem(seedPromptKey);
  const requestedPrompt = (seededPrompt || (typeof route.query.prompt === 'string' ? route.query.prompt : '')).slice(0, 8000);
  try {
    const [active, archived, profileResult, preferences] = await Promise.all([
      listAgentConversations('active'),
      listAgentConversations('archived'),
      listProviderProfiles(),
      getAgentPreferences(),
    ]);
    if (!isCurrent()) return;
    conversations.value = active.items;
    archivedConversations.value = archived.items;
    profiles.value = profileResult.items;
    const storedProfile = readPreference(profileStorageKey.value);
    const storedValid = planningProfiles.value.some((profile) => profile.id === storedProfile);
    selectedProfileId.value = storedValid
      ? storedProfile
      : (planningProfiles.value.some((profile) => profile.id === preferences.profile_id)
        ? preferences.profile_id ?? ''
        : planningProfiles.value[0]?.id ?? '');
    const storedThinking = readPreference(thinkingStorageKey.value) as AgentThinkingLevel;
    thinkingLevel.value = AGENT_THINKING_OPTIONS.some((item) => item.value === storedThinking)
      ? storedThinking
      : (preferences.thinking_level ?? 'medium');
    const requested = new URLSearchParams(window.location.search).get('conversation');
    const id = requested ?? (await restoreMainConversation()).conversation_id;
    if (!isCurrent()) return;
    await openConversation(id, true);
    if (isCurrent() && requestedPrompt) {
      message.value = requestedPrompt;
      await nextTick();
      composer.value?.focus();
    }
  } catch (error) {
    if (isCurrent()) setError(error, '无法加载对话助手。');
  } finally {
    if (isCurrent()) loading.value = false;
  }
}

function setError(error: unknown, fallback: string): void {
  if (!isAgentPage()) return;
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
  successMessage.value = '';
}

function setSuccess(message: string): void {
  successMessage.value = message;
  errorMessage.value = '';
}

async function refreshConversations(): Promise<void> {
  if (!isAgentPage()) return;
  const [active, archived] = await Promise.all([
    listAgentConversations('active'),
    listAgentConversations('archived'),
  ]);
  if (!isAgentPage()) return;
  conversations.value = active.items;
  archivedConversations.value = archived.items;
}

function applyLiveChangeSets(items: AgentTurn[], live: Record<string, ChangeSetSummary>): AgentTurn[] {
  if (!live) return items;
  return items.map((turn) => {
    const change = turn.metadata?.change_set;
    if (!change || typeof change !== 'object') return turn;
    const id = String((change as { id?: string }).id ?? '');
    if (!id || !live[id]) return turn;
    return { ...turn, metadata: { ...turn.metadata, change_set: live[id] } };
  });
}

function updateCache(
  summary: AgentConversationSummary,
  items: AgentTurn[],
  activeRun: string | null,
  failedRun: string | null,
): void {
  const cached: CachedAgentConversation = {
    conversation: summary,
    turns: items,
    latestSequence: latestSequence(items),
    activeRunId: activeRun,
    lastFailedRunId: failedRun,
    syncedAt: Date.now(),
  };
  putCachedConversation(userId.value, cached);
}

async function openConversation(id: string, fromInitialization = false): Promise<void> {
  if (!isAgentPage()) return;
  if (!fromInitialization) initializationRequest += 1;
  const request = ++conversationRequest;
  const isCurrent = () => isAgentPage() && request === conversationRequest;
  historyOpen.value = false;
  errorMessage.value = '';
  failedRunId.value = '';
  const cached = getCachedConversation(userId.value, id);
  if (cached) {
    currentConversation.value = cached.conversation;
    turns.value = cached.turns;
    failedRunId.value = cached.lastFailedRunId ?? '';
    loading.value = false;
    syncing.value = true;
    await nextTick();
    if (!isCurrent()) return;
    stickToBottom.value = true;
    jumpToBottom();
  } else {
    currentConversation.value = null;
    turns.value = [];
    loading.value = true;
    syncing.value = false;
  }
  try {
    let result = await getAgentConversation(id, cached ? cached.latestSequence : undefined);
    if (!isCurrent()) return;
    if (result.reset_required) {
      result = await getAgentConversation(id);
      if (!isCurrent()) return;
    }
    const merged = cached && !result.reset_required
      ? mergeTurns(cached.turns, result.turns)
      : result.turns;
    turns.value = applyLiveChangeSets(merged, result.change_sets);
    currentConversation.value = result.conversation;
    failedRunId.value = result.last_failed_run_id ?? '';
    updateCache(result.conversation, turns.value, result.active_run_id ?? null, failedRunId.value);
    loading.value = false;
    await nextTick();
    if (!isCurrent()) return;
    if (!cached || stickToBottom.value) {
      stickToBottom.value = true;
      jumpToBottom();
    }
    await router.replace({ path: '/agent', query: { conversation: id } });
    if (!isCurrent()) return;
    if (result.active_run_id && !sending.value) {
      sending.value = true;
      activeRunId.value = result.active_run_id;
      streamingRunId.value = result.active_run_id;
      streamingStartedAt.value = Date.now();
      void pollRun(result.active_run_id, id).finally(() => {
        sending.value = false;
        activeRunId.value = '';
        streamingRunId.value = '';
      });
    }
  } catch (error) {
    if (isCurrent()) setError(error, '无法恢复这段对话。');
  } finally {
    if (isCurrent()) {
      loading.value = false;
      syncing.value = false;
    }
  }
}

async function syncConversation(id: string, forceFull = false): Promise<void> {
  if (!isAgentPage()) return;
  const known = currentConversation.value?.id === id ? latestSequence(turns.value) : 0;
  const token = ++conversationRequest;
  syncing.value = true;
  try {
    let result = await getAgentConversation(id, !forceFull && known > 0 ? known : undefined);
    if (!isAgentPage() || token !== conversationRequest) return;
    if (result.reset_required) {
      result = await getAgentConversation(id);
      if (!isAgentPage() || token !== conversationRequest) return;
    }
    const merged = result.reset_required || forceFull || known === 0
      ? result.turns
      : mergeTurns(turns.value, result.turns);
    turns.value = applyLiveChangeSets(merged, result.change_sets);
    currentConversation.value = result.conversation;
    failedRunId.value = result.last_failed_run_id ?? '';
    updateCache(result.conversation, turns.value, result.active_run_id ?? null, failedRunId.value);
    if (stickToBottom.value) {
      await nextTick();
      if (isAgentPage() && token === conversationRequest) jumpToBottom();
    }
  } catch {
    // 离线时保留缓存；下一次轮询或手动操作会继续同步。
  } finally {
    if (isAgentPage() && token === conversationRequest) syncing.value = false;
  }
}

function handleTurnsScroll(): void {
  const element = turnsViewport.value;
  if (!element) return;
  stickToBottom.value = element.scrollHeight - element.scrollTop - element.clientHeight < 56;
}

function jumpToBottom(): void {
  const element = turnsViewport.value;
  if (!element) return;
  element.scrollTop = element.scrollHeight;
}

function scrollToBottom(smooth = true): void {
  const element = turnsViewport.value;
  if (!element) return;
  if (smooth) element.scrollTo({ top: element.scrollHeight, behavior: 'smooth' });
  else jumpToBottom();
}

function scrollToLatest(): void {
  stickToBottom.value = true;
  scrollToBottom(true);
}

function scheduleScrollToBottom(force = false): void {
  if (!force && !stickToBottom.value) return;
  window.cancelAnimationFrame(scrollFrame);
  scrollFrame = window.requestAnimationFrame(() => {
    void nextTick(() => {
      if (stickToBottom.value) jumpToBottom();
    });
  });
}

function newConversation(): void {
  if (!isAgentPage()) return;
  initializationRequest += 1;
  conversationRequest += 1;
  loading.value = false;
  syncing.value = false;
  showArchived.value = false;
  historyOpen.value = false;
  currentConversation.value = null;
  turns.value = [];
  failedRunId.value = '';
  errorMessage.value = '';
  successMessage.value = '';
  attachments.value = [];
  void router.replace('/agent');
  void nextTick(() => composer.value?.focus());
}

function useStarter(prompt: string): void {
  message.value = prompt;
  void nextTick(() => composer.value?.focus());
}

function updateProfile(id: string): void {
  selectedProfileId.value = id;
  writePreference(profileStorageKey.value, id);
}

function updateThinking(value: AgentThinkingLevel): void {
  thinkingLevel.value = value;
  writePreference(thinkingStorageKey.value, value);
}

async function openMainConversation(): Promise<void> {
  if (!isAgentPage()) return;
  const request = ++initializationRequest;
  const result = await restoreMainConversation();
  if (!isAgentPage() || request !== initializationRequest) return;
  await openConversation(result.conversation_id);
}

async function archiveCurrentConversation(): Promise<void> {
  const conversation = currentConversation.value;
  if (!conversation || conversation.status !== 'active' || conversationActionId.value) return;
  if (!window.confirm(`归档对话“${conversation.title}”？归档后仍可在“已归档”中恢复。`)) return;
  const request = ++initializationRequest;
  const isCurrent = () => isAgentPage() && request === initializationRequest;
  conversationActionId.value = conversation.id;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    await archiveAgentConversation(conversation.id, conversation.version);
    if (!isCurrent()) return;
    deleteCachedConversation(userId.value, conversation.id);
    await refreshConversations();
    if (!isCurrent()) return;
    showArchived.value = false;
    if (conversations.value.length) await openConversation(conversations.value[0].id);
    else {
      currentConversation.value = null;
      turns.value = [];
      await router.replace('/agent');
    }
    setSuccess('对话已归档，可在左侧“已归档”中恢复。');
  } catch (error) {
    setError(error, '归档对话失败，请刷新后重试。');
  } finally {
    conversationActionId.value = '';
  }
}

async function restoreConversation(conversation: AgentConversationSummary): Promise<void> {
  if (conversationActionId.value) return;
  const request = ++initializationRequest;
  const isCurrent = () => isAgentPage() && request === initializationRequest;
  conversationActionId.value = conversation.id;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const restored = await restoreAgentConversation(conversation.id, conversation.version);
    if (!isCurrent()) return;
    await refreshConversations();
    if (!isCurrent()) return;
    showArchived.value = false;
    await openConversation(restored.id);
    setSuccess('对话已恢复，可以继续发送消息。');
  } catch (error) {
    setError(error, '恢复对话失败，请刷新后重试。');
  } finally {
    conversationActionId.value = '';
  }
}

async function sendMessage(): Promise<void> {
  const text = message.value.trim() || (attachments.value.length ? '请阅读我上传的附件，结合当前对话帮我处理。' : '');
  if (!canSend.value || !text) return;
  sending.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  activeRunId.value = '';
  streamingRunId.value = '';
  streamingText.value = '';
  toolProgress.value = '正在提交…';
  try {
    const submitted = await submitAgentTurn({
      ...(currentConversation.value ? { conversation_id: currentConversation.value.id } : {}),
      ...(selectedProfileId.value ? { profile_id: selectedProfileId.value } : {}),
      thinking_level: thinkingLevel.value,
      idempotency_key: crypto.randomUUID(),
      message: text,
      attachment_ids: attachments.value.map((item) => item.id),
    });
    if (!isAgentPage()) return;
    message.value = '';
    attachments.value = [];
    activeRunId.value = submitted.run_id;
    streamingRunId.value = submitted.run_id;
    streamingStartedAt.value = Date.now();
    streamingText.value = '';
    toolProgress.value = '正在思考…';
    await refreshConversations();
    if (!isAgentPage()) return;
    await syncConversation(submitted.conversation_id);
    await pollRun(submitted.run_id, submitted.conversation_id);
  } catch (error) {
    setError(error, '发送失败，请稍后重试。');
  } finally {
    sending.value = false;
    activeRunId.value = '';
    streamingRunId.value = '';
    streamingText.value = '';
    toolProgress.value = '';
  }
}

async function attachFiles(files: File[]): Promise<void> {
  if (uploading.value || sending.value) return;
  if (attachments.value.length + files.length > 10) {
    errorMessage.value = '每条消息最多 10 个附件。';
    return;
  }
  uploading.value = true;
  try {
    for (const file of files) {
      let item = (await uploadAgentAttachment(file)).attachment;
      attachments.value.push(item);
      if (item.status !== 'ready') {
        for (let attempt = 0; attempt < 240 && !disposed; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 1000));
          item = (await getAgentAttachment(item.id)).attachment;
          const index = attachments.value.findIndex((value) => value.id === item.id);
          if (index >= 0) attachments.value[index] = item;
          if (['ready', 'failed', 'cancelled'].includes(item.status)) break;
        }
      }
    }
  } catch (error) {
    setError(error, '附件上传失败。');
  } finally {
    uploading.value = false;
  }
}

function removeAttachment(id: string): void {
  attachments.value = attachments.value.filter((item) => item.id !== id);
}

function turnAttachments(turn: AgentTurn): AgentAttachment[] {
  return Array.isArray(turn.metadata?.attachments) ? turn.metadata.attachments as AgentAttachment[] : [];
}

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

function runErrorLabel(code: string): string {
  return RUN_ERROR_LABELS[code] ?? code;
}

async function pollRun(runId: string, conversationId: string): Promise<void> {
  if (!isAgentPage()) return;
  let after = 0;
  streamingText.value = '';
  streamingReasoning.value = '';
  toolProgress.value = '正在思考…';
  streamingRunId.value = runId;
  activeRunId.value = runId;
  runStream?.abort();
  const stream = new AbortController();
  runStream = stream;
  const current = () => isAgentPage()
    && !stream.signal.aborted
    && (currentConversation.value?.id === conversationId || (!currentConversation.value && sending.value));
  while (!disposed && !stream.signal.aborted) {
    try {
      await streamAgentEvents(runId, after, (event) => {
        if (!current()) return;
        if (event.sequence <= after) return;
        after = event.sequence;
        if (event.type === 'text_delta') {
          streamingText.value += event.text ?? '';
          scheduleScrollToBottom();
        } else if (event.type === 'thinking_delta') {
          streamingReasoning.value = (streamingReasoning.value + (event.text ?? '')).slice(0, 20000);
          toolProgress.value = '正在思考…';
        } else if (event.type === 'tool_execution_start') {
          toolProgress.value = `正在执行：${toolLabel(event.tool_name ?? '')}`;
        } else if (event.type === 'tool_execution_end') {
          toolProgress.value = `已执行：${toolLabel(event.tool_name ?? '')}`;
        } else if (event.type === 'compaction_start') {
          toolProgress.value = '正在压缩较早上下文…';
        } else if (event.type === 'compaction_end') {
          toolProgress.value = event.success ? '上下文已压缩。' : '压缩未完成，原始记录保留。';
        }
        if (['text_delta', 'tool_execution_start', 'tool_execution_end'].includes(event.type)) {
          scheduleScrollToBottom();
        }
      }, stream.signal);
      if (!current()) return;
      const run = await getAgentRun(runId);
      if (!current()) return;
      if (['succeeded', 'failed', 'cancelled', 'expired'].includes(run.status)) {
        failedRunId.value = ['failed', 'expired'].includes(run.status) ? runId : '';
        // 最终记录就绪后再替换流式数据，避免网络等待期间清空整块回复。
        await syncConversation(conversationId);
        if (run.status === 'succeeded' && !turns.value.some(turn => turn.role === 'assistant' && turn.run_id === runId)) {
          // 临时同步失败时继续保留已经收到的回复，下一次重试成功后再结束流式状态。
          toolProgress.value = '正在同步已完成的回复…';
          await new Promise(resolve => window.setTimeout(resolve, 500));
          continue;
        }
        toolProgress.value = '';
        if (run.status !== 'succeeded') {
          errorMessage.value = run.error_code
            ? `本次运行未完成：${runErrorLabel(run.error_code)}。输入和历史已保存。`
            : '本次运行已停止。';
        }
        return;
      }
    } catch {
      if (stream.signal.aborted || disposed) return;
      toolProgress.value = '连接暂时中断，正在恢复；后台工作和输入已保存。';
    }
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
}

async function cancelRun(): Promise<void> {
  if (!activeRunId.value) return;
  try {
    await cancelAgentRun(activeRunId.value);
    setSuccess('已请求停止本轮，已完成的操作仍可查看和撤销。');
  } catch (error) {
    setError(error, '当前运行无法取消。');
  }
}

function isStreamingTurn(turn: AgentTurn): boolean {
  return turn.id.startsWith('streaming-');
}

function isAutoTurn(turn: AgentTurn): boolean {
  if (turn.role !== 'assistant' || isStreamingTurn(turn)) return false;
  if (turn.run_id && !userRunIds.value.has(turn.run_id)) return true;
  // 旧数据或后台任务可能没有 run_id；只接受明确的自动处理标记。
  return Boolean(turn.metadata?.automatic || turn.metadata?.notification_decision);
}

function turnTime(turn: AgentTurn): string {
  return formatRelativeDateTime(turn.created_at_epoch);
}

function conversationTime(conversation: AgentConversationSummary): string {
  return formatConversationDate(conversation.archived_at_epoch ?? conversation.updated_at_epoch);
}

async function copyTurn(turn: AgentTurn): Promise<void> {
  if (!(await copyText(turnDisplayContent(turn)))) {
    errorMessage.value = '复制失败，请手动选择文本。';
    return;
  }
  copiedTurnId.value = turn.id;
  window.setTimeout(() => {
    if (copiedTurnId.value === turn.id) copiedTurnId.value = '';
  }, 1600);
}

function turnDisplayContent(turn: AgentTurn): string {
  // 历史记录与流式显示相同的完整模型文字；后台业务仍保留独立的最终结果。
  return typeof turn.metadata.display_content === 'string' ? turn.metadata.display_content : turn.content;
}

function changeSetFor(turn: AgentTurn): ChangeSetSummary | null {
  const value = turn.metadata?.change_set;
  return value && typeof value === 'object' ? value as ChangeSetSummary : null;
}

function changeDetails(change: ChangeSetSummary): string[] {
  const operation = change.operations[0] as Record<string, unknown> | undefined;
  const detail = (operation?.entry ?? operation?.preview) as Record<string, unknown> | undefined;
  if (!detail) return [];
  const title = typeof detail.title === 'string' ? detail.title : '';
  const day = typeof detail.local_date === 'string' ? detail.local_date : '';
  const start = typeof detail.start_at === 'string' ? detail.start_at.slice(11, 16) : '';
  const end = typeof detail.end_at === 'string' ? detail.end_at.slice(11, 16) : '';
  return [
    ...(title ? [`标题：${title}`] : []),
    ...(day ? [`日期：${day}`] : []),
    ...(start && end ? [`时间：${start}–${end}`] : []),
  ];
}

async function applyChange(turn: AgentTurn, action: 'confirm' | 'reject' | 'undo'): Promise<void> {
  const change = changeSetFor(turn);
  if (!change) return;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const updated = action === 'confirm'
      ? await confirmChange(change)
      : action === 'reject'
        ? await rejectChange(change)
        : await undoChange(change.id);
    turn.metadata = { ...turn.metadata, change_set: updated };
    setSuccess(action === 'confirm' ? '已确认并应用日程变更。' : action === 'reject' ? '已拒绝候选变更。' : '已撤销这项日程变更。');
    if (currentConversation.value?.id) await syncConversation(currentConversation.value.id);
  } catch (error) {
    setError(error, '日程变更没有完成，请刷新后检查状态。');
  }
}

interface TurnCitation {
  kind?: string;
  id?: string | null;
  title?: string;
  href?: string | null;
  source_url?: string | null;
  quote?: string;
  version?: number | null;
}
function turnCitations(turn: AgentTurn): TurnCitation[] {
  const value = turn.metadata?.citations;
  return Array.isArray(value) ? value as TurnCitation[] : [];
}
function citationInternal(citation: TurnCitation): string | null {
  const href = citation.href ?? '';
  return href.startsWith('/') ? href : null;
}
function citationExternal(citation: TurnCitation): string | null {
  const url = citation.source_url ?? '';
  return url.startsWith('https://') ? url : null;
}

interface TurnPlanStep { index: number; title: string; status: string; }
interface TurnPlan {
  id: string; goal: string; status: string; steps: TurnPlanStep[];
  next_step?: TurnPlanStep | null; completed_count?: number; step_count?: number;
  receipt_count?: number; resumable?: boolean;
}
function turnPlan(turn: AgentTurn): TurnPlan | null {
  const value = turn.metadata?.plan;
  return value && typeof value === 'object' ? value as TurnPlan : null;
}
function planStepLabel(status: string): string {
  return ({ pending: '待执行', running: '进行中', done: '已完成', failed: '失败', waiting: '待补充信息' } as Record<string, string>)[status] ?? status;
}

// 账号切换时清理旧账号的内存缓存，避免对话正文串号。
watch(() => auth.user?.id, () => {
  clearConversationCache();
});
</script>

<template>
  <section class="agent-workspace" aria-labelledby="agent-heading">
    <header class="agent-topbar">
      <mdui-tooltip :content="sidebarOpen ? '收起会话' : '展开会话'">
        <mdui-button-icon aria-label="会话列表" @click="toggleSidebar">
          <AppIcon :name="sidebarOpen ? 'chevron_left' : 'menu'"></AppIcon>
        </mdui-button-icon>
      </mdui-tooltip>
      <div class="agent-topbar-title">
        <h1 id="agent-heading">{{ currentConversation?.title || '新对话' }}</h1>
        <small>
          <template v-if="currentConversation?.status === 'archived'">已归档</template>
          <template v-else-if="sending">正在回复</template>
          <template v-else>{{ currentConversation?.is_main ? '固定会话' : '对话助手' }}</template>
        </small>
      </div>
      <div class="agent-topbar-actions">
        <mdui-tooltip content="新建对话">
          <mdui-button-icon aria-label="新建对话" :disabled="sending" @click="newConversation">
            <AppIcon name="add"></AppIcon>
          </mdui-button-icon>
        </mdui-tooltip>
        <mdui-dropdown placement="bottom-end">
          <mdui-button-icon slot="trigger" aria-label="更多对话操作">
            <AppIcon name="more_vert"></AppIcon>
          </mdui-button-icon>
          <mdui-menu>
            <mdui-menu-item
              v-if="currentConversation?.status === 'active' && !currentConversation.is_main"
              :disabled="sending || Boolean(conversationActionId)"
              @click="archiveCurrentConversation"
            >归档当前对话</mdui-menu-item>
            <mdui-menu-item
              v-if="currentConversation?.status === 'archived'"
              @click="currentConversation && restoreConversation(currentConversation)"
            >恢复当前对话</mdui-menu-item>
            <mdui-menu-item
              v-if="currentConversation && !currentConversation.is_main"
              :disabled="sending"
              @click="openMainConversation"
            >回到主助手</mdui-menu-item>
            <mdui-menu-item @click="showArchived = !showArchived; historyOpen = true">
              {{ showArchived ? '查看会话' : '查看已归档' }}
            </mdui-menu-item>
          </mdui-menu>
        </mdui-dropdown>
      </div>
    </header>

    <div v-if="errorMessage" class="agent-inline-message agent-inline-message--error" role="alert">
      {{ errorMessage }}
      <mdui-button variant="text" @click="errorMessage = ''">关闭</mdui-button>
    </div>
    <div v-if="successMessage" class="agent-inline-message agent-inline-message--success" role="status">
      {{ successMessage }}
      <mdui-button variant="text" @click="successMessage = ''">关闭</mdui-button>
    </div>

    <div class="agent-body" :class="{ 'agent-body--sidebar-open': !mobileLayout && !sidebarCollapsed }">
      <div v-if="mobileLayout && historyOpen" class="agent-sidebar-backdrop" @click="historyOpen = false"></div>
      <aside
        class="agent-sidebar"
        :class="{ 'agent-sidebar--open': sidebarOpen }"
        :aria-hidden="!sidebarOpen"
        :inert="!sidebarOpen"
        aria-label="会话选择"
      >
        <header class="agent-sidebar-head">
          <div>
            <span>会话</span>
            <h2>{{ showArchived ? '已归档' : '历史对话' }}</h2>
          </div>
          <div class="agent-sidebar-head-actions">
            <mdui-button variant="text" @click="showArchived = !showArchived">
              {{ showArchived ? '返回' : `归档 ${archivedConversations.length}` }}
            </mdui-button>
            <mdui-button-icon aria-label="关闭会话列表" @click="closeSidebar">
              <AppIcon name="close"></AppIcon>
            </mdui-button-icon>
          </div>
        </header>
        <div class="agent-sidebar-scroll">
          <p v-if="!displayedConversations.length" class="agent-sidebar-empty">
            {{ showArchived ? '还没有归档对话。' : '发送第一条消息后，对话会保存在这里。' }}
          </p>
          <mdui-list>
            <mdui-list-item
              v-for="item in displayedConversations"
              :key="item.id"
              :headline="item.is_main ? '主助手 · 固定' : item.title"
              :active="currentConversation?.id === item.id"
              :disabled="sending || Boolean(conversationActionId)"
              rounded
              @click="openConversation(item.id)"
            >
              <span slot="description" class="agent-conversation-time">{{ conversationTime(item) }}</span>
            </mdui-list-item>
          </mdui-list>
        </div>
        <footer class="agent-sidebar-footer">
          <mdui-list>

            <mdui-list-item href="/agent/workspace" @click.prevent="router.push('/agent/workspace')">
              <AppIcon slot="icon" name="monitoring"></AppIcon>
              运行工作台
            </mdui-list-item>

          </mdui-list>
        </footer>
      </aside>

      <div class="agent-conversation-shell">
        <div ref="turnsViewport" class="agent-message-scroll" aria-live="polite" @scroll.passive="handleTurnsScroll">
          <div v-if="loading && !turns.length" class="agent-initial-loading" role="status">
            <mdui-circular-progress></mdui-circular-progress>
            <span>正在恢复对话…</span>
          </div>
          <div v-else-if="!turns.length && !sending" class="agent-welcome">
            <span class="agent-welcome-mark" aria-hidden="true"><AppIcon name="smart_toy"></AppIcon></span>
            <p class="eyebrow">知序对话助手</p>
            <h2>今天想先处理什么？</h2>
            <p class="agent-welcome-copy">我会先读取真实日程、事务和通知，再给出可执行的下一步。写入前会先说明。</p>
            <div v-if="!planningProfiles.length" class="agent-model-warning">
              <AppIcon name="hub"></AppIcon>
              <span>还没有可用的规划模型，先到设置里连接一个。</span>
              <mdui-button variant="text" @click="router.push('/settings/models')">去设置</mdui-button>
            </div>
            <div class="agent-starters" aria-label="开场问题">
              <mdui-chip
                v-for="starter in starterPrompts"
                :key="starter.label"
                variant="assist"
                class="agent-starter"
                :disabled="sending"
                @click="useStarter(starter.prompt)"
              >
                {{ starter.label }}
              </mdui-chip>
            </div>
          </div>

          <!-- 同一运行的助手消息保持节点身份，保留滚动位置及用户展开的思考区域。 -->
          <template v-for="turn in displayedTurns" :key="turn.role === 'assistant' && turn.run_id ? `assistant-${turn.run_id}` : turn.id">
            <AgentAutoCard v-if="isAutoTurn(turn)" :turn="turn" />
            <article v-else class="agent-turn" :class="`agent-turn--${turn.role}`" :data-role="turn.role">
              <div class="agent-turn-label">
                <span v-if="turn.role === 'assistant'" class="agent-turn-avatar"><AppIcon name="smart_toy"></AppIcon></span>
                <span>{{ turn.role === 'user' ? '你' : '知序助手' }}</span>
                <time>{{ turnTime(turn) }}</time>
              </div>

              <template v-if="turn.role === 'user'">
                <div class="agent-turn-content agent-turn-content--user">{{ turn.content }}</div>
              </template>
              <template v-else>
                <AgentProcess
                  :metadata="turn.metadata"
                  :streaming="isStreamingTurn(turn)"
                  :tool-progress="isStreamingTurn(turn) ? toolProgress : ''"
                />
                <AgentMarkdown
                  v-if="turn.content || !isStreamingTurn(turn)"
                  class="agent-turn-content"
                  :content="turnDisplayContent(turn)"
                  :streaming="isStreamingTurn(turn)"
                />
                <div v-else class="agent-stream-placeholder">
                  <AgentThinkingDots />
                  <span>{{ toolProgress || '正在思考…' }}</span>
                </div>
              </template>

              <div v-for="attachment in turnAttachments(turn)" :key="attachment.id" class="agent-attachment">
                <img v-if="attachment.image_url" :src="attachment.image_url" :alt="attachment.filename" />
                <span>{{ attachment.filename }}</span>
              </div>

              <RouterLink
                v-if="turn.role === 'assistant' && turn.metadata?.source_id && changeSetFor(turn)"
                class="app-link agent-turn-source"
                :to="{ path: '/inbox', query: { candidate: changeSetFor(turn)!.id } }"
              >查看通知原文 / 修改处理结果</RouterLink>

              <section v-if="turnPlan(turn)" class="agent-plan-card" aria-label="执行计划">
                <strong>执行计划：{{ turnPlan(turn)!.goal }}</strong>
                <p class="agent-plan-meta">
                  已完成 {{ turnPlan(turn)!.completed_count ?? 0 }} / {{ turnPlan(turn)!.step_count ?? turnPlan(turn)!.steps.length }} 步
                  <template v-if="turnPlan(turn)!.receipt_count"> · 写操作 {{ turnPlan(turn)!.receipt_count }} 条</template>
                </p>
                <details>
                  <summary>查看计划步骤</summary>
                  <ol>
                    <li v-for="step in turnPlan(turn)!.steps" :key="step.index" :data-status="step.status">
                      <span>{{ step.index + 1 }}. {{ step.title }}</span>
                      <small>{{ planStepLabel(step.status) }}</small>
                    </li>
                  </ol>
                </details>
              </section>

              <section v-if="turnCitations(turn).length" class="agent-citations" aria-label="本轮引用">
                <strong>本轮引用</strong>
                <ul>
                  <li v-for="(citation, index) in turnCitations(turn)" :key="(citation.kind ?? '') + ':' + (citation.id ?? index)">
                    <RouterLink
                      v-if="citationInternal(citation) && !citationInternal(citation)!.startsWith('/api/')"
                      class="app-link"
                      :to="citationInternal(citation)!"
                    >{{ citation.title || '查看资料' }}</RouterLink>
                    <a
                      v-else-if="citationInternal(citation)"
                      class="app-link"
                      :href="citationInternal(citation)!"
                    >{{ citation.title || '下载附件' }}</a>
                    <a
                      v-else-if="citationExternal(citation)"
                      class="app-link"
                      :href="citationExternal(citation)!"
                      target="_blank"
                      rel="noopener noreferrer"
                    >{{ citation.title || '打开原网页' }}</a>
                    <span v-else>{{ citation.title || '资料引用' }}</span>
                    <small v-if="citation.quote">{{ citation.quote }}</small>
                  </li>
                </ul>
              </section>

              <section v-if="changeSetFor(turn)" class="agent-change-card" aria-label="日程变更预览">
                <strong>{{ changeSetFor(turn)?.summary }}</strong>
                <ul v-if="changeSetFor(turn) && changeDetails(changeSetFor(turn)!).length">
                  <li v-for="detail in changeDetails(changeSetFor(turn)!)" :key="detail">{{ detail }}</li>
                </ul>
                <p>状态：{{ statusLabel(changeSetFor(turn)?.status ?? '') }}。写入前请检查日程与时间。</p>
                <div class="agent-change-actions">
                  <mdui-button v-if="changeSetFor(turn)?.status === 'proposed'" variant="filled" @click="applyChange(turn, 'confirm')">确认应用</mdui-button>
                  <mdui-button v-if="changeSetFor(turn)?.status === 'proposed'" variant="outlined" @click="applyChange(turn, 'reject')">拒绝</mdui-button>
                  <mdui-button v-if="changeSetFor(turn)?.status === 'confirmed'" variant="outlined" @click="applyChange(turn, 'undo')">撤销已应用变更</mdui-button>
                </div>
              </section>

              <!-- 输出期间保留操作栏高度，完成时只显示按钮，不再把消息框撑高。 -->
              <footer v-if="turn.role === 'assistant'" class="agent-turn-actions" :style="isStreamingTurn(turn) ? { visibility: 'hidden' } : undefined" :inert="isStreamingTurn(turn) || undefined">
                <mdui-tooltip :content="copiedTurnId === turn.id ? '已复制' : '复制回复'">
                  <mdui-button-icon :aria-label="copiedTurnId === turn.id ? '已复制' : '复制回复'" @click="copyTurn(turn)">
                    <AppIcon :name="copiedTurnId === turn.id ? 'check' : 'content_copy'"></AppIcon>
                  </mdui-button-icon>
                </mdui-tooltip>
              </footer>
            </article>
          </template>

          <div v-if="syncing && turns.length && !sending" class="agent-sync-status" role="status">
            <mdui-circular-progress></mdui-circular-progress>
            <span>正在同步新内容…</span>
          </div>
        </div>

        <mdui-fab
          v-if="showScrollLatest"
          class="agent-scroll-latest"
          size="small"
          variant="surface"
          aria-label="回到最新"
          @click="scrollToLatest"
        >
          <AppIcon slot="icon" name="arrow_downward"></AppIcon>
        </mdui-fab>

        <div v-if="currentConversation?.status === 'archived'" class="agent-archive-banner" role="status">
          <span>这段对话已归档，内容和工具记录仍然保留，但不能继续发送。</span>
          <mdui-button variant="outlined" :loading="conversationActionId === currentConversation.id" @click="restoreConversation(currentConversation)">
            恢复对话
          </mdui-button>
        </div>

        <AgentComposer
          v-if="currentConversation?.status !== 'archived'"
          ref="composer"
          :message="message"
          :attachments="attachments"
          :profiles="planningProfiles"
          :selected-profile-id="selectedProfileId"
          :thinking-level="thinkingLevel"
          :sending="sending"
          :uploading="uploading"
          :can-send="canSend"
          :dragging-files="draggingFiles"
          @update:message="message = $event"
          @update:selected-profile-id="updateProfile"
          @update:thinking-level="updateThinking"
          @send="sendMessage"
          @attach="attachFiles"
          @remove-attachment="removeAttachment"
          @cancel="cancelRun"
          @dragging="draggingFiles = $event"
        />
      </div>
    </div>
  </section>
</template>
