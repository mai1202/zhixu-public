<script setup lang="ts">
import { computed, ref } from 'vue';
import { AGENT_THINKING_OPTIONS, type AgentAttachment, type AgentThinkingLevel } from '../../api/agent';
import type { ProviderProfile } from '../../api/models';
import { readMduiValue } from '../../utils/mdui';
import AppIcon from '../AppIcon.vue';
import AgentThinkingDots from './AgentThinkingDots.vue';

const props = defineProps<{
  message: string;
  attachments: AgentAttachment[];
  profiles: ProviderProfile[];
  selectedProfileId: string;
  thinkingLevel: AgentThinkingLevel;
  sending: boolean;
  uploading: boolean;
  canSend: boolean;
  draggingFiles: boolean;
}>();

const emit = defineEmits<{
  'update:message': [value: string];
  'update:selectedProfileId': [value: string];
  'update:thinkingLevel': [value: AgentThinkingLevel];
  send: [];
  attach: [files: File[]];
  removeAttachment: [id: string];
  cancel: [];
  dragging: [value: boolean];
}>();

const fileInput = ref<HTMLInputElement | null>(null);
const cameraInput = ref<HTMLInputElement | null>(null);
const messageInput = ref<(HTMLElement & { focus?: () => void }) | null>(null);

const profileLabel = computed(() => {
  const profile = props.profiles.find((item) => item.id === props.selectedProfileId);
  return profile ? profile.label || profile.model_id : '选择模型';
});
const thinkingLabel = computed(() => (
  AGENT_THINKING_OPTIONS.find((item) => item.value === props.thinkingLevel)?.label.replace(/（推荐）/, '') ?? '思考强度'
));

function focus(): void {
  messageInput.value?.focus?.();
}

function chooseFiles(event: Event): void {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (files.length) emit('attach', files);
  input.value = '';
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (props.canSend) emit('send');
}

function selectProfile(id: string): void {
  emit('update:selectedProfileId', id);
}

function selectThinking(value: AgentThinkingLevel): void {
  emit('update:thinkingLevel', value);
}

function onPaste(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.files ?? []);
  if (!files.length) return;
  event.preventDefault();
  emit('attach', files);
}

defineExpose({ focus });
</script>

<template>
  <div
    class="agent-composer"
    :class="{ 'agent-composer--dragging': draggingFiles }"
    @dragover.prevent="emit('dragging', true)"
    @dragleave.self="emit('dragging', false)"
    @drop.prevent="emit('dragging', false); emit('attach', Array.from($event.dataTransfer?.files ?? []))"
    @paste="onPaste"
  >
    <div v-if="draggingFiles" class="agent-drop-hint">
      <AppIcon name="upload_file"></AppIcon>
      松开即可上传资料
    </div>
    <div class="agent-composer-field">
      <label class="sr-only" for="agent-message">消息</label>
      <mdui-text-field
        id="agent-message"
        ref="messageInput"
        class="agent-message-field"
        variant="outlined"
        rows="1"
        autosize
        max-rows="8"
        enterkeyhint="send"
        maxlength="32000"
        placeholder="输入消息，或添加附件"
        :value="message"
        :disabled="sending"
        @input="emit('update:message', readMduiValue($event))"
        @keydown="onKeydown"
      ></mdui-text-field>
      <div class="agent-composer-field-actions">
        <input
          ref="fileInput"
          type="file"
          multiple
          hidden
          accept=".png,.jpg,.jpeg,.txt,.md,.csv,.json,.log,.pdf,.docx,.xls,.xlsx"
          :disabled="sending || uploading"
          @change="chooseFiles"
        />
        <mdui-tooltip content="添加资料">
          <mdui-button-icon
            aria-label="添加资料"
            :disabled="sending || uploading"
            @click="fileInput?.click()"
          >
            <AppIcon name="attach_file"></AppIcon>
          </mdui-button-icon>
        </mdui-tooltip>
        <!-- 手机浏览器可直接拍照，图片仍经过同一附件上传、归属与OCR流程。 -->
        <input ref="cameraInput" type="file" hidden accept="image/*" capture="environment" :disabled="sending || uploading" @change="chooseFiles" />
        <mdui-button-icon class="agent-camera-button" aria-label="拍照上传" :disabled="sending || uploading" @click="cameraInput?.click()">
          <AppIcon name="photo_camera"></AppIcon>
          <span class="sr-only">拍照上传</span>
        </mdui-button-icon>
        <mdui-button-icon
          class="agent-send-button"
          variant="filled"
          aria-label="发送消息"
          :disabled="!canSend"
          @click="emit('send')"
        >
          <AppIcon name="arrow_upward"></AppIcon>
        </mdui-button-icon>
      </div>
    </div>

    <div v-if="attachments.length" class="agent-composer-files">
      <span v-for="item in attachments" :key="item.id" class="agent-file-chip">
        <AppIcon name="description"></AppIcon>
        <span>{{ item.filename }}</span>
        <small>{{ item.status === 'ready' ? '就绪' : item.status === 'failed' ? '识别失败' : '处理中' }}</small>
        <button type="button" :disabled="uploading" aria-label="移除附件" @click="emit('removeAttachment', item.id)">×</button>
      </span>
    </div>

    <div v-if="sending" class="agent-composer-tools">
      <div class="agent-composer-pickers">
        <mdui-dropdown placement="top-start">
          <mdui-button slot="trigger" variant="text" :disabled="sending || !profiles.length">
            <AppIcon slot="icon" name="hub"></AppIcon>
            {{ profileLabel }}
          </mdui-button>
          <mdui-menu>
            <mdui-menu-item
              v-for="profile in profiles"
              :key="profile.id"
              @click="selectProfile(profile.id)"
            >
              {{ profile.label }} · {{ profile.model_id }}
            </mdui-menu-item>
          </mdui-menu>
        </mdui-dropdown>
        <mdui-dropdown placement="top-start">
          <mdui-button slot="trigger" variant="text" :disabled="sending">
            <AppIcon slot="icon" name="tune"></AppIcon>
            {{ thinkingLabel }}
          </mdui-button>
          <mdui-menu>
            <mdui-menu-item
              v-for="option in AGENT_THINKING_OPTIONS"
              :key="option.value"
              @click="selectThinking(option.value)"
            >
              {{ option.label }}
            </mdui-menu-item>
          </mdui-menu>
        </mdui-dropdown>
      </div>
      <span v-if="!profiles.length" class="agent-composer-warning">先配置规划模型</span>
      <span v-else class="agent-composer-note">日程写入前会先预览</span>
      <AgentThinkingDots v-if="sending" />
      <mdui-button v-if="sending" variant="text" @click="emit('cancel')">停止</mdui-button>
    </div>
  </div>
</template>
