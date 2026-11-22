<script setup lang="ts">
import AgentTaskPlans from '../components/AgentTaskPlans.vue';
import { computed, onMounted, onBeforeUnmount, ref } from 'vue';
import { useRoute } from 'vue-router';
import { apiRequest, ApiError } from '../api/client';
import {
  AGENT_THINKING_OPTIONS,
  addAgentMemory,
  deleteAgentMemory,
  getAgentPreferences,
  listAgentMemories,
  type AgentMemory,
  type AgentPreferences,
} from '../api/agent';
import { listProviderProfiles, type ProviderProfile } from '../api/models';
import AppIcon from '../components/AppIcon.vue';

// 设置中心复用同一份表单与保存接口，避免自动处理开关出现两套行为。
const route = useRoute();

const preferences = ref<AgentPreferences>({ profile_id: null, automatic_notifications: true, context_budget: 64000, thinking_level: 'medium' });
const profiles = ref<ProviderProfile[]>([]);
const memories = ref<AgentMemory[]>([]);
const busy = ref(false);
const loading = ref(true);
const deletingMemoryId = ref('');
const addingMemory = ref(false);
const memoryQuery = ref('');
const newMemoryContent = ref('');
const newMemoryCategory = ref<AgentMemory['category']>('preference');
const memoryNotice = ref('');
const error = ref('');
const saved = ref(false);
let disposed = false;
let loadToken = 0;
// 独立路由直接展开原来的分区，不再嵌套选项卡。
const activeSettingsTab = computed(() => String(route.meta.settingsSection ?? 'assistant'));
const sectionTitle = computed(() => ({notifications: '通知处理', assistant: '助手推理', memory: '长期记忆', schedules: '定时任务'} as Record<string, string>)[activeSettingsTab.value]);
const savedSnapshot = ref('');
const filteredMemories = computed(() => {
  const query = memoryQuery.value.trim().toLocaleLowerCase();
  if (!query) return memories.value;
  return memories.value.filter((memory) => `${memoryCategoryLabel(memory.category)} ${memory.content}`.toLocaleLowerCase().includes(query));
});
async function loadSettings() {
  const token = ++loadToken;
  const snapshot = JSON.stringify(preferences.value);
  loading.value = true;
  try {
    const [preferenceResult, profileResult, memoryResult] = await Promise.all([
      getAgentPreferences(),
      listProviderProfiles(),
      listAgentMemories(),
    ]);
    if (disposed || token !== loadToken || snapshot !== JSON.stringify(preferences.value)) return;
    preferences.value = preferenceResult;
    savedSnapshot.value = JSON.stringify(preferenceResult);
    profiles.value = profileResult.items.filter(item => item.purpose === 'planning' && item.enabled);
    memories.value = memoryResult.items;
  } catch (failure) { if (!disposed && token === loadToken) error.value = failure instanceof ApiError ? failure.message : '设置加载失败。'; }
  finally { if (!disposed && token === loadToken) loading.value = false; }
}
function refreshFromAgent() {
  // 助手修改后同步已保存值；用户尚未保存的表单不被迟到响应覆盖。
  if (JSON.stringify(preferences.value) === savedSnapshot.value && !busy.value) void loadSettings();
}
onMounted(() => { void loadSettings(); window.addEventListener('settings-updated', refreshFromAgent); });
onBeforeUnmount(() => { disposed = true; loadToken++; window.removeEventListener('settings-updated', refreshFromAgent); });
async function save(): Promise<void> {
  // 托管意愿与模型一起保存；缺少模型时显示等待配置，不伪造已执行结果。
  if (loading.value || busy.value) return;
  busy.value = true; error.value = ''; saved.value = false;
  try {
    const patch = activeSettingsTab.value === 'notifications'
      ? {automatic_notifications: preferences.value.automatic_notifications}
      : {profile_id: preferences.value.profile_id, thinking_level: preferences.value.thinking_level, context_budget: preferences.value.context_budget};
    preferences.value = await apiRequest<AgentPreferences>('/api/v1/agent/preferences', {method: 'PATCH', body: JSON.stringify(patch)});
    savedSnapshot.value = JSON.stringify(preferences.value); saved.value = true;
  }
  catch (failure) { error.value = failure instanceof ApiError ? failure.message : '保存失败。'; }
  finally { busy.value = false; }
}
async function removeMemory(memory: AgentMemory): Promise<void> {
  // 删除长期记忆是不可逆操作，先向用户确认具体内容。
  if (!window.confirm(`删除这条长期记忆？\n\n${memory.content}`)) return;
  deletingMemoryId.value = memory.id;
  error.value = '';
  try {
    await deleteAgentMemory(memory.id);
    memories.value = memories.value.filter(item => item.id !== memory.id);
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure.message : '长期记忆删除失败。';
  } finally {
    deletingMemoryId.value = '';
  }
}
function memoryCategoryLabel(category: AgentMemory['category']): string {
  const labels: Record<AgentMemory['category'], string> = {
    preference: '偏好', constraint: '限制', routine: '作息', goal: '目标', other: '其他',
  };
  return labels[category];
}
async function addMemory(): Promise<void> {
  const content = newMemoryContent.value.trim();
  if (!content || addingMemory.value) return;
  addingMemory.value = true;
  error.value = '';
  memoryNotice.value = '';
  try {
    const result = await addAgentMemory(content, newMemoryCategory.value);
    const index = memories.value.findIndex((memory) => memory.id === result.memory.id);
    if (index >= 0) memories.value[index] = result.memory;
    else memories.value.unshift(result.memory);
    newMemoryContent.value = '';
    memoryNotice.value = '长期记忆已保存。';
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure.message : '长期记忆保存失败。';
  } finally {
    addingMemory.value = false;
  }
}

function readElementValue(event: Event): string {
  // MDUI 选择框和文本框都把新值同步到宿主元素，避免在模板里散落类型断言。
  return String((event.currentTarget as HTMLElement & { value?: string }).value ?? '');
}

function changeProfile(event: Event): void {
  saved.value = false;
  preferences.value.profile_id = readElementValue(event) || null;
}

function changeThinking(event: Event): void {
  saved.value = false;
  preferences.value.thinking_level = readElementValue(event) as AgentPreferences['thinking_level'];
}

function changeBudget(event: Event): void {
  saved.value = false;
  const value = Number(readElementValue(event));
  if (Number.isFinite(value)) preferences.value.context_budget = value;
}

function changeMemoryCategory(event: Event): void {
  newMemoryCategory.value = readElementValue(event) as AgentMemory['category'];
}

function changeAutomaticNotifications(event: Event): void {
  saved.value = false;
  preferences.value.automatic_notifications = Boolean(
    (event.currentTarget as HTMLElement & { checked?: boolean }).checked,
  );
}
</script>
<template>
  <section class="page-section assistant-settings">
    <header class="assistant-settings-header">
      <div class="assistant-settings-title">
        <RouterLink class="assistant-settings-back" to="/settings" aria-label="返回设置目录">
          <AppIcon name="chevron_left"></AppIcon>
        </RouterLink>
        <div>
          <p class="eyebrow">设置</p>
          <h1>{{ sectionTitle }}</h1>
          <p>管理当前设置，保存后持续生效。</p>
        </div>
      </div>
      <mdui-button v-if="['assistant', 'notifications'].includes(activeSettingsTab)" variant="filled" :loading="busy || loading" :disabled="busy || loading" @click="save">保存设置</mdui-button>
    </header>
    <p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <p v-if="saved" role="status" class="success-text">主助手设置已保存。</p>

    <div v-if="['assistant', 'notifications'].includes(activeSettingsTab)" class="assistant-settings-panel">
      <mdui-card v-if="activeSettingsTab === 'notifications'" class="assistant-setting-card" variant="outlined">
        <div class="assistant-setting-toggle">
          <div>
            <span class="assistant-setting-icon"><AppIcon name="task_alt"></AppIcon></span>
            <div>
              <h2>自动把通知更新为事务和课程变更</h2>
              <p>开启后按通知证据登记或更新待办、调课和已授权提醒；关闭后仍可整理标题与摘要，但不自动改动事务和课程。</p>
            </div>
          </div>
          <mdui-switch
            aria-label="自动把通知更新为事务和课程变更"
            :checked="preferences.automatic_notifications"
            :disabled="busy || loading"
            @change="changeAutomaticNotifications"
          ></mdui-switch>
        </div>
        <p class="assistant-setting-note">本人发送或转发的微信消息按采集器核实的账号身份排除，不纳入通知，也不自动生成事务与提醒。未知身份不按昵称猜测。</p>
        <p class="assistant-setting-note">当前选择：{{ preferences.automatic_notifications ? '自动维护事务和课程' : '仅整理标题与摘要' }}；点击上方“保存设置”后生效。只处理新到达的通知，不自动重跑历史通知。</p>
        <p v-if="preferences.automatic_notifications && !preferences.profile_id" class="assistant-setting-note" role="status">默认自动维护已开启，配置主助手模型后才会开始处理。</p>
        <p class="assistant-setting-note">对话和通知内容会发送给你选择的模型服务。其他新聊天不会替换固定的主助手。</p>
      </mdui-card>

      <mdui-card v-if="activeSettingsTab === 'assistant'" class="assistant-setting-card" variant="outlined">
        <div class="assistant-setting-card-heading">
          <span class="assistant-setting-icon"><AppIcon name="hub"></AppIcon></span>
          <div>
            <h2>规划与推理</h2>
            <p>标准及以上会核对日期、冲突和长期偏好；不支持推理的模型会自动降级。</p>
          </div>
        </div>
        <div class="assistant-setting-grid">
          <mdui-select
            variant="outlined"
            label="主助手模型"
            :value="preferences.profile_id ?? ''"
            @change="changeProfile"
          >
            <mdui-menu-item value="">暂未选择</mdui-menu-item>
            <mdui-menu-item v-for="profile in profiles" :key="profile.id" :value="profile.id">
              {{ profile.label }} · {{ profile.model_id }}
            </mdui-menu-item>
          </mdui-select>
          <mdui-select
            variant="outlined"
            label="思考强度"
            :value="preferences.thinking_level"
            @change="changeThinking"
          >
            <mdui-menu-item v-for="option in AGENT_THINKING_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </mdui-menu-item>
          </mdui-select>
          <mdui-select
            variant="outlined"
            label="上下文容量"
            :value="String(preferences.context_budget)"
            @change="changeBudget"
          >
            <mdui-menu-item value="64000">64K（默认）</mdui-menu-item>
            <mdui-menu-item value="256000">256K</mdui-menu-item>
            <mdui-menu-item value="1000000">1M（需模型支持）</mdui-menu-item>
          </mdui-select>
        </div>
        <p class="assistant-setting-note">接近预算时会压缩较早对话，保留事实、偏好、决定和未完成任务；原始记录完整保存，助手仍可检索。</p>

      </mdui-card>
    </div>

    <div v-if="activeSettingsTab === 'memory'" class="assistant-settings-panel">
      <mdui-card class="assistant-setting-card" variant="outlined">
        <div class="assistant-setting-card-heading">
          <span class="assistant-setting-icon"><AppIcon name="description"></AppIcon></span>
          <div>
            <h2>跨对话记忆</h2>
            <p>稳定偏好、限制、作息和目标会在这里持续生效；临时安排仍以日程数据为准。</p>
          </div>
        </div>
        <p v-if="memoryNotice" class="success-text" role="status">{{ memoryNotice }}</p>
        <form class="assistant-memory-form" @submit.prevent="addMemory">
          <mdui-text-field
            label="补充一条长期记忆"
            :value="newMemoryContent"
            maxlength="600"
            placeholder="例如：周三晚上不安排活动"
            @input="newMemoryContent = readElementValue($event)"
            @keydown.enter.prevent="addMemory"
          ></mdui-text-field>
          <mdui-select
            variant="outlined"
            label="分类"
            :value="newMemoryCategory"
            @change="changeMemoryCategory"
          >
            <mdui-menu-item value="preference">偏好</mdui-menu-item>
            <mdui-menu-item value="constraint">限制</mdui-menu-item>
            <mdui-menu-item value="routine">作息</mdui-menu-item>
            <mdui-menu-item value="goal">目标</mdui-menu-item>
            <mdui-menu-item value="other">其他</mdui-menu-item>
          </mdui-select>
          <mdui-button type="submit" variant="outlined" :loading="addingMemory" :disabled="addingMemory || !newMemoryContent.trim()">添加记忆</mdui-button>
        </form>
      </mdui-card>

      <mdui-card class="assistant-setting-card" variant="outlined">
        <mdui-text-field
          v-if="memories.length"
          class="assistant-memory-search"
          label="筛选长期记忆"
          type="search"
          :value="memoryQuery"
          placeholder="搜索偏好、作息或目标"
          clearable
          @input="memoryQuery = readElementValue($event)"
        ></mdui-text-field>
        <div v-if="filteredMemories.length" class="assistant-memory-list">
          <div v-for="memory in filteredMemories" :key="memory.id" class="assistant-memory-row">
            <span>{{ memoryCategoryLabel(memory.category) }}</span>
            <p>{{ memory.content }}</p>
            <mdui-button
              type="button"
              variant="text"
              :loading="deletingMemoryId === memory.id"
              :disabled="Boolean(deletingMemoryId)"
              @click="removeMemory(memory)"
            >删除</mdui-button>
          </div>
        </div>
        <p v-else-if="memories.length" class="helper-text">没有匹配的长期记忆。</p>
        <p v-else class="helper-text">还没有保存长期记忆。可以手动添加，也可以直接在对话中表达稳定偏好。</p>
      </mdui-card>
    </div>

    <div v-if="activeSettingsTab === 'schedules'" class="assistant-settings-panel">
      <AgentTaskPlans />
    </div>
  </section>
</template>

<style scoped>
/* 原嵌入设置分区改为独立路由，统一沿用外层布局与大标题。 */
.assistant-settings { width: 100%; }
</style>
