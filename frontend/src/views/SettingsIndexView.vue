<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { apiRequest, ApiError } from '../api/client';
import AppIcon from '../components/AppIcon.vue';
import { readMduiValue } from '../utils/mdui';
interface SettingsEntry { title: string; href: string; description: string; keywords: string }
interface SettingsGroup { title: string; entries: SettingsEntry[] }
const router = useRouter();
const groups = ref<SettingsGroup[]>([]);
const search = ref('');
const error = ref('');
// 根据真实设置入口分配语义图标，避免所有条目都显示同一个设置符号。
const entryIcons: Record<string, string> = {
  devices: 'computer', notifications: 'notifications', models: 'hub', assistant: 'smart_toy',
  memory: 'memory', schedules: 'calendar_month', mail: 'mail', account: 'account_circle',
  security: 'shield', diagnostics: 'monitoring',
};
// 一个项目只保留一个目录条目；助手读取同一份服务端目录。
const visibleGroups = computed(() => groups.value.map(group => ({ ...group,
  entries: group.entries.filter(entry => !search.value.trim() ||
    (entry.title + entry.description + entry.keywords).toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase())),
})).filter(group => group.entries.length));
onMounted(async () => {
  try { groups.value = (await apiRequest<{ groups: SettingsGroup[] }>('/api/v1/settings/catalog')).groups; }
  catch (failure) { error.value = failure instanceof ApiError ? failure.message : '设置目录加载失败，请刷新重试。'; }
});
function askSettings() {
  // 只打开同一个主助手窗口，不自动发送请求或切换页面。
  window.dispatchEvent(new CustomEvent('open-main-agent', {detail: {
    context: '设置目录 /settings；请先读取 get_user_settings 中的 catalog，按真实入口引导；按用户要求用设置工具修改。',
    prompt: '',
  }}));
}
</script>
<template>
  <section class="page-section settings-index">
    <header><p class="eyebrow">按需要管理你的知序</p><h1>设置</h1></header>
    <RouterLink to="/mobile" class="app-link mobile-guide-link">在手机使用知序：安装、分享文字与上传截图 →</RouterLink>
    <section class="settings-guide" aria-label="设置助手">
      <AppIcon name="smart_toy" />
      <div><h2>告诉助手你想调整什么</h2><p>帮你找到设置入口，也可以按你的要求修改采集范围、助手偏好和提醒。</p></div>
      <mdui-button variant="filled" @click="askSettings">让助手帮我设置</mdui-button>
    </section>
    <mdui-text-field
      class="settings-search-label"
      label="查找设置"
      type="search"
      clearable
      :value="search"
      placeholder="例如：附件、自动通知、思考强度"
      @input="search = readMduiValue($event)"
    ></mdui-text-field>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <section v-for="group in visibleGroups" :key="group.title" class="settings-group">
      <h2>{{ group.title }}</h2>
      <mdui-list>
        <mdui-list-item
          v-for="entry in group.entries"
          :key="entry.href"
          :headline="entry.title"
          :description="entry.description"
          rounded
          @click="router.push(entry.href)"
        >
          <AppIcon slot="icon" :name="entryIcons[entry.href.split('/').pop() ?? ''] ?? 'help'"></AppIcon>
          <AppIcon slot="end-icon" name="chevron_right"></AppIcon>
        </mdui-list-item>
      </mdui-list>
    </section>
    <p v-if="groups.length && !visibleGroups.length" class="helper-text">没有匹配设置，可以让助手帮你查找。</p>
  </section>
</template>
<style scoped>
.settings-index{max-width:1024px}.settings-guide{display:flex;align-items:center;gap:20px;padding:24px;border-radius:20px;background:var(--app-surface-container,#f0f3fb);margin:24px 0}.settings-guide>div{flex:1}.settings-guide h2{margin:0 0 8px;font-size:20px}.settings-guide p{margin:0;line-height:1.7}.settings-search-label{display:block;margin:24px 0}.settings-group{margin:24px 0}.settings-group h2{font-size:18px}.settings-group mdui-list-item{--mdui-typescale-label-large-size:13px;--mdui-typescale-body-medium-size:11px}@media(max-width:600px){.settings-guide{flex-wrap:wrap;padding:18px}.settings-guide>div{min-width:190px}.settings-guide mdui-button{width:100%}}
</style>
