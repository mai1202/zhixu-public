<script setup lang="ts">
import { computed } from 'vue';
import { renderMarkdown } from '../../utils/markdown';

const props = defineProps<{
  content: string;
  streaming?: boolean;
}>();

const html = computed(() => renderMarkdown(props.content, Boolean(props.streaming)));
</script>

<template>
  <div class="agent-markdown" v-html="html"></div>
</template>
