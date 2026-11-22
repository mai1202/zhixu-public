<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import {
  dismissInsight,
  listInsights,
  scanInsights,
  type AgentInsightItem,
  type InsightModule,
} from '../api/insights';
import AppIcon from './AppIcon.vue';
import RippleSurface from './RippleSurface.vue';
import { useAuthStore } from '../stores/auth';

const props = withDefaults(defineProps<{
  module: InsightModule;
  emptyText?: string;
  // 日程页需要把日历铺满，空预警时隐藏整块面板而不是留下大块空白。
  hideEmpty?: boolean;
  // 紧凑模式用于工具栏下方的页面级提示，减少卡片占用高度。
  compact?: boolean;
  // 事务/通知页在手机端默认折叠，避免长卡片把页面撑开。
  collapsible?: boolean;
}>(), {
  emptyText: '当前没有需要主动处理的事实型预警。',
  hideEmpty: false,
  compact: false,
  collapsible: false,
});

const router = useRouter();
const auth = useAuthStore();
const items = ref<AgentInsightItem[]>([]);
const counts = ref({ critical: 0, warning: 0, info: 0 });
const loading = ref(false);
const busyId = ref('');
const errorMessage = ref('');
const expanded = ref(!props.collapsible || !window.matchMedia('(max-width: 760px)').matches);

const visibleItems = computed(() => items.value.slice(0, 4));
const summary = computed(() => {
  const parts = [];
  if (counts.value.critical) parts.push(`${counts.value.critical} 项紧急`);
  if (counts.value.warning) parts.push(`${counts.value.warning} 项需关注`);
  if (counts.value.info) parts.push(`${counts.value.info} 项变更`);
  return parts.join(' · ') || '当前没有高优先级预警';
});

onMounted(() => void refresh(true));

async function refresh(runScan = false): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  try {
    if (runScan) await scanInsights();
    const result = await listInsights(props.module);
    items.value = result.items;
    counts.value = result.counts;
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : 'Agent 建议暂时无法更新。';
  } finally {
    loading.value = false;
  }
}

async function dismiss(item: AgentInsightItem): Promise<void> {
  if (busyId.value) return;
  busyId.value = item.id;
  try {
    await dismissInsight(item);
    items.value = items.value.filter((entry) => entry.id !== item.id);
    counts.value = {
      critical: items.value.filter((entry) => entry.severity === 'critical').length,
      warning: items.value.filter((entry) => entry.severity === 'warning').length,
      info: items.value.filter((entry) => entry.severity === 'info').length,
    };
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '这条建议暂时不能关闭。';
  } finally {
    busyId.value = '';
  }
}

function askAgent(): void {
  const context = visibleItems.value.map((item, index) => (
    `${index + 1}. [${item.severity}] ${item.title}：${item.detail}`
  ));
  const prompt = [
    `请以主 Agent 身份检查我的${props.module === 'course' ? '课程' : props.module === 'task' ? '事务' : '通知'}状态。`,
    context.length ? `后台扫描发现：\n${context.join('\n')}` : '当前后台扫描没有高优先级预警，请主动查询近期待办、课程冲突和最新通知，安排下一步。',
    '请先查询真实数据，再按紧急程度给出不超过 5 项的行动建议；必要时直接维护任务或课程，并说明实际写入了什么。',
  ].join('\n\n');
  sessionStorage.setItem(`agent-seeded-prompt:${auth.user?.id ?? ''}`, prompt);
  void router.push('/agent');
}

function severityLabel(value: string): string {
  return ({ critical: '紧急', warning: '关注', info: '变更' } as Record<string, string>)[value] ?? value;
}

/** 整行与“查看详情”都跳到同一条事实的详情页；没有详情地址时整行不可跳转。 */
function openItem(item: AgentInsightItem): void {
  if (!item.action_href) return;
  void router.push(item.action_href);
}
</script>

<template>
  <section
    v-if="!hideEmpty || items.length || errorMessage"
    class="agent-insight-panel"
    :class="{ 'agent-insight-panel--compact': compact }"
    :data-module="module"
    aria-label="Agent 主动建议"
  >
    <header>
      <span class="agent-insight-mark"><AppIcon name="smart_toy"></AppIcon></span>
      <div>
        <p class="eyebrow">Agent 值班扫描</p>
        <h2>现在值得处理的事</h2>
        <p>{{ summary }}</p>
      </div>
      <div class="agent-insight-actions">
        <mdui-button variant="text" :disabled="loading" @click="refresh(true)">重新扫描</mdui-button>
        <mdui-button variant="filled" @click="askAgent">交给主 Agent</mdui-button>
        <mdui-button v-if="collapsible" variant="text" @click="expanded = !expanded">
          {{ expanded ? '收起' : '展开' }}
        </mdui-button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <template v-if="!collapsible || expanded">
      <div v-if="loading && !items.length" class="agent-insight-loading"><mdui-circular-progress></mdui-circular-progress></div>
      <ol v-else-if="visibleItems.length" class="agent-insight-list">
      <li v-for="item in visibleItems" :key="item.id" :data-severity="item.severity">
        <RippleSurface
          class="agent-insight-row"
          :label="item.action_href ? `查看详情：${item.title}` : item.title"
          :disabled="!item.action_href"
          @activate="openItem(item)"
        >
          <span class="agent-insight-dot" aria-hidden="true"></span>
          <span class="agent-insight-body">
            <span class="agent-insight-title">
              <strong>{{ item.title }}</strong>
              <span>{{ severityLabel(item.severity) }}</span>
            </span>
            <span class="agent-insight-detail">{{ item.detail }}</span>
          </span>
        </RippleSurface>
        <div class="agent-insight-row-actions">
          <!-- 用带底色的 tonal 按钮，避免用户把「查看详情」误认成未初始化的普通链接。 -->
          <mdui-button v-if="item.action_href" variant="tonal" @click="openItem(item)">查看详情</mdui-button>
          <mdui-button variant="text" :disabled="Boolean(busyId)" @click="dismiss(item)">
            {{ busyId === item.id ? '关闭中…' : '已知晓' }}
          </mdui-button>
        </div>
      </li>
      </ol>
      <p v-else class="agent-insight-empty">{{ emptyText }}</p>
    </template>
  </section>
</template>
