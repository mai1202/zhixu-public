<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import { ApiError } from '../api/client';
import { getAgentConversation, getAgentRun, restoreMainConversation, submitAgentTurn,
  streamAgentEvents, uploadAgentAttachment, type AgentAttachment, type AgentTurn } from '../api/agent';
import AppIcon from './AppIcon.vue';
import AgentProcess from './agent/AgentProcess.vue';

const props = defineProps<{ context: string; hideLauncher?: boolean }>();
const open = ref(false);
const draft = ref('');
const messages = ref<AgentTurn[]>([]);
const attachments = ref<AgentAttachment[]>([]);
const conversationId = ref('');
const busy = ref(false);
// 小窗同样保留模型返回的实时思考，默认收起，完成后沿用同一消息节点。
const liveRun = ref('');
const liveText = ref('');
const liveReasoning = ref('');
const liveStarted = ref(0);
const displayedMessages = computed<AgentTurn[]>(() => busy.value && liveRun.value && !messages.value.some(turn => turn.role === 'assistant' && turn.run_id === liveRun.value)
  ? [...messages.value, { id: `streaming-${liveRun.value}`, run_id: liveRun.value, role: 'assistant', sequence: 0,
    content: liveText.value, metadata: { reasoning: liveReasoning.value }, created_at_epoch: liveStarted.value }] : messages.value);
const uploading = ref(false);
const error = ref('');
const selectedContext = ref('');
const scroller = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const canSend = computed(() => !busy.value && !uploading.value && Boolean(draft.value.trim() || attachments.value.length));
let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
let submissionKey = crypto.randomUUID();
let attemptedPayload = '';
let eventCursor = 0;
let eventStream: AbortController | undefined;

function assistantMarkdown(content: string): string {
  // 与完整对话保持相同的安全渲染，附件下载链接和列表在小窗中也能正常使用。
  return DOMPurify.sanitize(marked.parse(content) as string);
}
function fail(failure: unknown) { error.value = failure instanceof ApiError ? failure.message : '助手暂时无法连接，消息历史会保留。'; }
function citations(turn: AgentTurn): Array<{ title?: string; href?: string | null; source_url?: string | null; quote?: string; version?: number | null }> {
  return Array.isArray(turn.metadata.citations) ? turn.metadata.citations as Array<{ title?: string; href?: string | null; source_url?: string | null; quote?: string; version?: number | null }> : [];
}
function internalHref(item: { href?: string | null }): string | null {
  return (item.href ?? '').startsWith('/') ? item.href ?? null : null;
}
function externalHref(item: { source_url?: string | null }): string | null {
  return (item.source_url ?? '').startsWith('https://') ? item.source_url ?? null : null;
}
function elementValue(event: Event): string {
  // MDUI 输入框把值放在自定义元素上，输入事件统一在这里转换为字符串。
  return String((event.currentTarget as HTMLElement & { value?: string }).value ?? '');
}
function removeAttachment(id: string): void {
  attachments.value = attachments.value.filter((item) => item.id !== id);
}
async function load() {
  if (disposed) return;
  clearTimeout(timer);
  try {
    conversationId.value = (await restoreMainConversation()).conversation_id;
    const detail = await getAgentConversation(conversationId.value);
    if (disposed) return;
    messages.value = detail.turns.slice(-12);
    await nextTick();
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
    if (detail.active_run_id) { busy.value = true; void poll(detail.active_run_id); }
  } catch (failure) { if (!disposed) fail(failure); }
}
async function toggle() {
  open.value = !open.value;
  if (open.value) { selectedContext.value = props.context; await load(); }
}
async function poll(runId: string) {
  if (liveRun.value !== runId) {
    liveRun.value = runId; liveText.value = ''; liveReasoning.value = ''; eventCursor = 0; liveStarted.value = Math.floor(Date.now() / 1000);
  }
  eventStream?.abort();
  const stream = new AbortController(); eventStream = stream;
  try {
    await streamAgentEvents(runId, eventCursor, event => {
      if (disposed || stream.signal.aborted || event.sequence <= eventCursor) return;
      eventCursor = event.sequence;
      if (event.type === 'text_delta') liveText.value += event.text ?? '';
      if (event.type === 'thinking_delta') liveReasoning.value = (liveReasoning.value + (event.text ?? '')).slice(0, 20000);
    }, stream.signal);
    const run = await getAgentRun(runId);
    if (disposed || stream.signal.aborted) return;
    if (['succeeded','failed','cancelled','expired'].includes(run.status)) {
      if (run.status === 'failed' || run.status === 'expired') error.value = run.error_code === 'platform_token_quota_exceeded'
        ? '今日模型预算已用完或已预留，请查看计费明细并等待北京时间零点重置。' : '本轮处理未完成，可在主助手历史里重试。';
      const detail = await getAgentConversation(conversationId.value);
      if (!disposed) {
        messages.value = detail.turns.slice(-12);
        busy.value = false;
        // 设置修改完成后通知表单重新核对；不代替用户点击入口或改变当前路由。
        if (run.status === 'succeeded') window.dispatchEvent(new CustomEvent('settings-updated'));
      }
    } else timer = setTimeout(() => { void poll(runId); }, 1200);
  } catch (failure) { if (!disposed && !stream.signal.aborted) { fail(failure); timer = setTimeout(() => { void poll(runId); }, 1200); } }
}
async function send() {
  if (!canSend.value) return;
  busy.value = true; error.value = '';
  try {
    if (!conversationId.value) conversationId.value = (await restoreMainConversation()).conversation_id;
    // 页面索引只作为资料上下文；实际业务权限仍来自本轮用户输入及账户设置。
    const message = draft.value.trim() || '请阅读这些附件并给我建议。';
    const pageContext = (selectedContext.value || props.context).slice(0, 4000);
    const signature = JSON.stringify([message, pageContext, attachments.value.map(item => item.id)]);
    if (attemptedPayload !== signature) { submissionKey = crypto.randomUUID(); attemptedPayload = signature; }
    const result = await submitAgentTurn({conversation_id: conversationId.value,
      message, page_context: pageContext, attachment_ids: attachments.value.map(item => item.id), idempotency_key: submissionKey});
    submissionKey = crypto.randomUUID(); draft.value = ''; attachments.value = [];
    const detail = await getAgentConversation(conversationId.value);
    if (!disposed) messages.value = detail.turns.slice(-12);
    void poll(result.run_id);
  } catch (failure) { busy.value = false; fail(failure); }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  uploading.value = true; error.value = '';
  try {
    for (const file of files) {
      if (attachments.value.length >= 10) throw new Error('最多选择 10 个附件。');
      const result = await uploadAgentAttachment(file);
      attachments.value.push(result.attachment);
    }
  } catch (failure) { fail(failure); }
  finally { uploading.value = false; input.value = ''; }
}
function onRequest(event: Event) {
  const detail = (event as CustomEvent<{context?: string; prompt?: string}>).detail;
  selectedContext.value = detail?.context || props.context;
  draft.value = detail?.prompt || draft.value;
  open.value = true; void load();
}
watch(() => props.context, () => { selectedContext.value = props.context; });
watch([() => messages.value.at(-1)?.id, liveText], () => {
  // 只有用户原本位于末尾时跟随输出；正在回看历史时不抢滚动位置。
  const viewport = scroller.value;
  if (viewport && viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 56) requestAnimationFrame(() => {
    if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
  });
});
onMounted(() => window.addEventListener('open-main-agent', onRequest));
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); eventStream?.abort(); window.removeEventListener('open-main-agent', onRequest); });
</script>

<template>
  <div class="main-agent-chat">
    <mdui-fab v-if="!open && !hideLauncher" class="main-agent-launcher" variant="primary" extended @click="toggle">
      <AppIcon slot="icon" name="smart_toy"></AppIcon>
      问主助手
    </mdui-fab>
    <!-- 隐藏重复启动按钮时仍保持窗口关闭，必须由用户明确打开。 -->
    <section v-else-if="open" class="main-agent-window" aria-label="与主助手沟通">
      <header>
        <div class="main-agent-heading">
          <span class="main-agent-mark"><AppIcon name="smart_toy"></AppIcon></span>
          <div>
            <strong>主助手</strong>
            <small>同步到同一段历史 · 已带入当前页面上下文</small>
          </div>
        </div>
        <!-- 收起会立即卸载按钮；原生提示不留下 MDUI 浮层的迟到动画。 -->
        <mdui-button-icon aria-label="收起主助手" title="收起主助手" @click="open = false">
          <AppIcon name="close"></AppIcon>
        </mdui-button-icon>
      </header>
      <div ref="scroller" class="main-agent-messages" aria-live="polite">
        <div v-if="!messages.length" class="main-agent-empty">
          <span><AppIcon name="chat"></AppIcon></span>
          <strong>就当前页面继续</strong>
          <p>可以帮你查找设置、整理通知或修改安排；按你的明确要求执行，并说明实际结果。</p>
        </div>
        <article v-for="turn in displayedMessages" :key="turn.role === 'assistant' && turn.run_id ? `assistant-${turn.run_id}` : turn.id" :data-role="turn.role"><small>{{ turn.role === 'user' ? '你' : '主助手' }}</small>
          <p v-if="turn.role === 'user'">{{ turn.content }}</p>
          <template v-else>
            <AgentProcess :metadata="turn.metadata" :streaming="turn.id.startsWith('streaming-')" />
            <div class="main-agent-markdown" v-html="assistantMarkdown(typeof turn.metadata.display_content === 'string' ? turn.metadata.display_content : turn.content)"></div>
          </template>
          <div v-if="citations(turn).length" class="main-agent-citations">
            <strong>引用</strong>
            <span v-for="(item, index) in citations(turn)" :key="index">
              <RouterLink v-if="internalHref(item) && !internalHref(item)!.startsWith('/api/')" :to="internalHref(item)!">{{ item.title || '查看资料' }}</RouterLink>
              <a v-else-if="internalHref(item)" :href="internalHref(item)!">{{ item.title || '下载附件' }}</a>
              <a v-else-if="externalHref(item)" :href="externalHref(item)!" target="_blank" rel="noopener noreferrer">{{ item.title || '原网页' }}</a>
              <em v-else>{{ item.title || '资料' }}</em>
            </span>
          </div></article>
        <div v-if="busy" class="main-agent-thinking" role="status">
          <mdui-circular-progress></mdui-circular-progress>
          <span>正在结合当前页面处理…</span>
        </div>
      </div>
      <p v-if="error" class="error-text" role="alert">{{ error }}</p>
      <div v-if="attachments.length" class="main-agent-files">
        <mdui-chip
          v-for="file in attachments"
          :key="file.id"
          variant="input"
          deletable
          @delete="removeAttachment(file.id)"
        >
          <AppIcon slot="icon" name="description"></AppIcon>
          {{ file.filename }}
        </mdui-chip>
      </div>
      <form @submit.prevent="send">
        <mdui-text-field
          class="main-agent-input"
          variant="filled"
          rows="2"
          autosize
          max-rows="5"
          maxlength="16000"
          aria-label="发给主助手"
          placeholder="就当前页面聊聊，或直接告诉我需要修改什么……"
          :value="draft"
          @input="draft = elementValue($event)"
        ></mdui-text-field>
        <input ref="fileInput" type="file" multiple hidden @change="upload" />
        <div class="main-agent-tools">
          <mdui-button variant="text" type="button" :disabled="uploading || busy" @click="fileInput?.click()">
            <AppIcon slot="icon" name="attach_file"></AppIcon>
            {{ uploading ? '上传中…' : '添加附件' }}
          </mdui-button>
          <RouterLink :to="{path:'/agent', query:{conversation:conversationId || undefined}}">完整对话</RouterLink>
          <mdui-button type="submit" variant="filled" :disabled="!canSend">
            <AppIcon slot="icon" name="arrow_upward"></AppIcon>
            发送
          </mdui-button>
        </div>
      </form>
    </section>
  </div>
</template>

<style scoped>
/* 浮动主助手沿用 MDUI 色彩和形状，扩展后的小窗提供更大的阅读与输入空间。 */
.main-agent-chat {
  position: fixed;
  z-index: 60;
  right: 24px;
  bottom: 24px;
}

.main-agent-launcher {
  --mdui-typescale-label-large-size: 13px;
}

.main-agent-launcher .app-icon {
  width: 20px;
  height: 20px;
}

.main-agent-window {
  display: flex;
  width: min(560px, calc(100vw - 32px));
  height: min(760px, 86dvh);
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgb(var(--mdui-color-outline-variant, 226 229 237));
  border-radius: 28px;
  background: rgb(var(--mdui-color-surface, 255 255 255));
  box-shadow: 0 18px 60px rgb(29 42 68 / 20%);
  animation: main-agent-window-in .24s cubic-bezier(.2, 0, 0, 1) both;
}

.main-agent-window header {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 14px 12px 16px;
  border-bottom: 1px solid color-mix(in srgb, rgb(var(--mdui-color-outline-variant, 226 229 237)) 76%, transparent);
  background: rgb(var(--mdui-color-surface-container-low, 247 248 252));
}

.main-agent-heading {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.main-agent-heading > div {
  display: grid;
  min-width: 0;
  gap: 1px;
}

.main-agent-heading strong {
  font-size: 15px;
}

.main-agent-heading small {
  overflow: hidden;
  color: rgb(var(--mdui-color-on-surface-variant, 96 104 120));
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.main-agent-mark {
  display: grid;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  place-items: center;
  border-radius: 12px;
  background: color-mix(in srgb, rgb(var(--mdui-color-primary, 49 93 168)) 12%, white);
  color: rgb(var(--mdui-color-primary, 49 93 168));
}

.main-agent-mark .app-icon {
  width: 19px;
  height: 19px;
}

.main-agent-messages {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  overflow-y: auto;
  padding: 18px;
  overscroll-behavior: contain;
  scrollbar-width: thin;
}

.main-agent-messages article {
  margin-bottom: 17px;
  animation: main-agent-message-in .24s ease-out both;
}

.main-agent-messages article p {
  margin: 5px 0;
  line-height: 1.65;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.main-agent-messages article[data-role='user'] {
  margin-left: 34px;
  padding: 10px 13px;
  border-radius: 16px 16px 5px 16px;
  background: color-mix(in srgb, rgb(var(--mdui-color-primary, 49 93 168)) 10%, white);
}

.main-agent-messages article[data-role='assistant'] .main-agent-markdown {
  margin-top: 5px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, rgb(var(--mdui-color-outline-variant, 226 229 237)) 82%, transparent);
  border-radius: 5px 15px 15px 15px;
  background: rgb(var(--mdui-color-surface-container-low, 247 248 252));
}

.main-agent-messages small {
  color: rgb(var(--mdui-color-on-surface-variant, 96 104 120));
  font-size: 10px;
}

.main-agent-empty {
  display: grid;
  justify-items: center;
  gap: 5px;
  margin: auto;
  padding: 40px 18px 26px;
  color: rgb(var(--mdui-color-on-surface-variant, 96 104 120));
  text-align: center;
}

.main-agent-empty > span {
  display: grid;
  width: 48px;
  height: 48px;
  place-items: center;
  margin-bottom: 4px;
  border-radius: 17px;
  background: color-mix(in srgb, rgb(var(--mdui-color-primary, 49 93 168)) 10%, white);
  color: rgb(var(--mdui-color-primary, 49 93 168));
}

.main-agent-empty .app-icon {
  width: 24px;
  height: 24px;
}

.main-agent-empty strong {
  color: rgb(var(--mdui-color-on-surface, 28 34 48));
  font-size: 15px;
}

.main-agent-empty p {
  max-width: 320px;
  margin: 0;
  font-size: 11px;
  line-height: 1.55;
}

.main-agent-thinking {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 5px 2px;
  color: rgb(var(--mdui-color-on-surface-variant, 96 104 120));
  font-size: 12px;
}

.main-agent-thinking mdui-circular-progress {
  width: 18px;
  height: 18px;
}

.main-agent-window form {
  flex: 0 0 auto;
  padding: 10px 12px 11px;
  border-top: 1px solid color-mix(in srgb, rgb(var(--mdui-color-outline-variant, 226 229 237)) 76%, transparent);
  background: rgb(var(--mdui-color-surface, 255 255 255));
}

.main-agent-input {
  width: 100%;
  --mdui-typescale-body-large-size: 13px;
}

.main-agent-input::part(container) {
  min-height: 48px;
  border-radius: 14px;
  background: rgb(var(--mdui-color-surface-container-low, 247 248 252));
}

.main-agent-tools {
  display: flex;
  align-items: center;
  gap: 7px;
  margin-top: 6px;
}

.main-agent-tools > a {
  margin-left: auto;
  color: rgb(var(--mdui-color-primary, 49 93 168));
  font-size: 11px;
  font-weight: 500;
  text-decoration: none;
}

.main-agent-tools mdui-button {
  --mdui-typescale-label-large-size: 11px;
}

.main-agent-tools .app-icon {
  width: 16px;
  height: 16px;
}

.main-agent-files {
  display: flex;
  max-height: 84px;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: 5px;
  overflow: auto;
  padding: 7px 12px 0;
}

.main-agent-files mdui-chip {
  --mdui-typescale-label-large-size: 10px;
}

.main-agent-files .app-icon {
  width: 14px;
  height: 14px;
}

.main-agent-window > .error-text {
  margin: 6px 12px;
  font-size: 11px;
}

/* 模型正文按段落排版，长链接和代码不撑破移动端对话窗口。 */
.main-agent-markdown {
  overflow-wrap: anywhere;
  line-height: 1.68;
}

.main-agent-markdown :deep(p) {
  margin: 7px 0;
  white-space: normal;
}

.main-agent-markdown :deep(pre) {
  max-width: 100%;
  overflow-x: auto;
  padding: 10px;
  border-radius: 10px;
  background: rgb(var(--mdui-color-surface-container-high, 236 239 245));
}

.main-agent-markdown :deep(table) {
  display: block;
  max-width: 100%;
  overflow-x: auto;
}

.main-agent-markdown :deep(a) {
  color: rgb(var(--mdui-color-primary, 49 93 168));
}

.main-agent-markdown :deep(img) {
  max-width: 100%;
  height: auto;
}

@keyframes main-agent-window-in {
  from {
    opacity: 0;
    transform: translateY(8px) scale(.98);
  }
  to {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

@keyframes main-agent-message-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (max-width: 760px) {
  .main-agent-chat {
    right: 12px;
    bottom: calc(84px + env(safe-area-inset-bottom));
  }

  .main-agent-window {
    right: 10px;
    left: 10px;
    width: auto;
    height: min(680px, calc(100dvh - 170px - env(safe-area-inset-bottom)));
    border-radius: 24px;
  }

  .main-agent-messages {
    padding: 14px;
  }

  .main-agent-heading small {
    max-width: 210px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .main-agent-window,
  .main-agent-messages article {
    animation: none;
  }
}
</style>
