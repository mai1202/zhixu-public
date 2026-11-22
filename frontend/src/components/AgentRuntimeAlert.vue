<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { getAgentRuntimeHealth, type AgentRuntimeHealth } from '../api/agentRuntime';
import { formatRelativeDateTime } from '../utils/datetime';
import AppIcon from './AppIcon.vue';

/**
 * Agent 无法正常工作时的首页提示。
 *
 * 通知没有被整理、事务标题像原文，通常是模型调用失败；这里把「额度 / 密钥 / 连接」
 * 这类可处理的原因直接说清楚，并给出对应设置入口，而不是让用户猜。
 */
const props = withDefaults(defineProps<{
  // 事务页只放一条紧凑提示，避免和事务清单抢空间。
  compact?: boolean;
}>(), {
  compact: false,
});

const router = useRouter();
const health = ref<AgentRuntimeHealth | null>(null);
const dismissed = ref(false);

const visible = computed(() => Boolean(health.value && !health.value.healthy && !dismissed.value));
const fatal = computed(() => ['no_profile', 'quota_exhausted', 'key_rejected'].includes(health.value?.status ?? ''));
const failureTime = computed(() => {
  const epoch = health.value?.last_failure_epoch;
  return epoch ? formatRelativeDateTime(epoch) : '';
});

onMounted(async () => {
  try {
    health.value = await getAgentRuntimeHealth();
  } catch {
    // 诊断接口失败时不影响页面其它内容，也不把网络错误伪装成 Agent 故障。
    health.value = null;
  }
});

function dismiss(): void {
  dismissed.value = true;
}

function openSettings(): void {
  const href = health.value?.action_href || '/settings/models';
  void router.push(href);
}
</script>

<template>
  <section
    v-if="visible && health"
    class="agent-runtime-alert"
    :class="{ 'agent-runtime-alert--compact': compact }"
    :data-status="health.status"
    role="alert"
    aria-live="polite"
  >
    <span class="agent-runtime-alert-icon" aria-hidden="true">
      <AppIcon :name="fatal ? 'error' : 'warning'"></AppIcon>
    </span>
    <div class="agent-runtime-alert-copy">
      <span class="agent-runtime-alert-kicker">Agent 暂时无法正常工作</span>
      <h2>{{ health.title }}</h2>
      <p>{{ health.detail }}</p>
      <small v-if="failureTime">
        最近一次失败：{{ failureTime }}
        <template v-if="health.last_failure_code"> · {{ health.last_failure_code }}</template>
      </small>
    </div>
    <div class="agent-runtime-alert-actions">
      <mdui-button variant="filled" @click="openSettings">{{ health.action_label }}</mdui-button>
      <mdui-button variant="text" @click="router.push('/agent/workspace')">查看模型账单</mdui-button>
      <mdui-button v-if="!fatal" variant="text" @click="dismiss">稍后处理</mdui-button>
    </div>
  </section>
</template>

<style scoped>
.agent-runtime-alert {
  display: grid;
  grid-template-columns: 44px minmax(0, 1fr);
  gap: 14px;
  margin: 16px 0;
  padding: 18px 20px;
  border: 1px solid #f0d3ae;
  border-radius: 18px;
  background: linear-gradient(135deg, #fff8ee, #fffdfa 62%);
}

.agent-runtime-alert[data-status="key_rejected"],
.agent-runtime-alert[data-status="no_profile"] {
  border-color: #efc9c4;
  background: linear-gradient(135deg, #fdf1ef, #fffbfa 62%);
}

.agent-runtime-alert-icon {
  display: grid;
  width: 44px;
  height: 44px;
  place-items: center;
  border-radius: 14px;
  background: #fbe6cd;
  color: #9a5a12;
}

.agent-runtime-alert[data-status="key_rejected"] .agent-runtime-alert-icon,
.agent-runtime-alert[data-status="no_profile"] .agent-runtime-alert-icon {
  background: #fadfdb;
  color: #a33c34;
}

.agent-runtime-alert-copy {
  min-width: 0;
}

.agent-runtime-alert-kicker {
  display: block;
  color: #9a5a12;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.04em;
}

.agent-runtime-alert-copy h2 {
  margin: 4px 0 6px;
  font-size: 1.1rem;
  line-height: 1.4;
}

.agent-runtime-alert-copy p {
  margin: 0;
  color: #5d5346;
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.agent-runtime-alert-copy small {
  display: block;
  margin-top: 6px;
  color: #8a7a66;
  font-size: 11px;
}

.agent-runtime-alert-actions {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.agent-runtime-alert--compact {
  padding: 12px 14px;
  border-radius: 14px;
  grid-template-columns: 32px minmax(0, 1fr);
}

.agent-runtime-alert--compact .agent-runtime-alert-icon {
  width: 32px;
  height: 32px;
  border-radius: 10px;
}

.agent-runtime-alert--compact .agent-runtime-alert-copy h2 {
  margin: 2px 0 4px;
  font-size: 0.98rem;
}

.agent-runtime-alert--compact .agent-runtime-alert-copy p {
  font-size: 12px;
}

@media (max-width: 640px) {
  .agent-runtime-alert {
    grid-template-columns: 32px minmax(0, 1fr);
    padding: 12px 14px;
  }

  .agent-runtime-alert-icon {
    width: 32px;
    height: 32px;
    border-radius: 10px;
  }

  .agent-runtime-alert-actions {
    justify-content: flex-start;
  }
}
</style>
