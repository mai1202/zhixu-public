<script setup lang="ts">
import { computed } from 'vue';
import { materialSymbols } from './materialSymbols';

const props = defineProps<{
  name: string;
}>();

// 内置常用线性图标，改为 Google 官方 Material Symbols，避免依赖外部字体或网络资源。
// 设置使用齿轮；筛选与调参继续使用独立的 tune 图标。
// 状态提示：warning 用于可自行处理的失败，error 用于必须立即处理的失败。
const paths = computed(() => materialSymbols[props.name] ?? materialSymbols.help);
</script>

<template>
  <svg class="app-icon" viewBox="0 -960 960 960" fill="currentColor" aria-hidden="true">
    <path v-for="path in paths" :key="path" :d="path"></path>
  </svg>
</template>
