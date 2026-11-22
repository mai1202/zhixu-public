<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink } from 'vue-router';
import type { AgentTurn } from '../../api/agent';
import { formatRelativeDateTime } from '../../utils/datetime';
import AppIcon from '../AppIcon.vue';

const props = defineProps<{ turn: AgentTurn }>();

const metadata = computed(() => props.turn.metadata ?? {});
const changeSet = computed(() => {
  const value = metadata.value.change_set;
  return value && typeof value === 'object' ? value as { id?: string; summary?: string } : null;
});
const summary = computed(() => {
  const source = String(changeSet.value?.summary ?? props.turn.content ?? '')
    .replace(/[#>*_`~\-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return source.length > 110 ? `${source.slice(0, 110)}…` : source || 'Agent 已自动处理这条信息。';
});
const sourceId = computed(() => String(metadata.value.source_id ?? ''));
const link = computed(() => {
  if (sourceId.value) return `/inbox/${encodeURIComponent(sourceId.value)}`;
  return null;
});
</script>

<template>
  <article class="agent-auto-card">
    <span class="agent-auto-icon" aria-hidden="true"><AppIcon name="smart_toy"></AppIcon></span>
    <div class="agent-auto-body">
      <div class="agent-auto-heading">
        <strong>自动处理</strong>
        <time>{{ formatRelativeDateTime(turn.created_at_epoch) }}</time>
      </div>
      <p>{{ summary }}</p>
    </div>
    <RouterLink v-if="link" class="agent-auto-link" :to="link">
      查看
      <AppIcon name="chevron_right"></AppIcon>
    </RouterLink>
  </article>
</template>
