<script setup lang="ts">
import { computed } from 'vue';
import { readMduiValue } from '../utils/mdui';
const props = withDefaults(defineProps<{ modelValue: string; label?: string }>(), { label: '时区' });
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
// 用地区名称说明时区；保留已有的其他时区，不静默替换用户配置。
const choices = [['Asia/Shanghai', '中国标准时间（北京）'], ['Asia/Singapore', '新加坡时间'],
  ['Asia/Hong_Kong', '香港时间'], ['Asia/Tokyo', '日本时间'], ['Europe/London', '英国时间（自动夏令时）'],
  ['America/New_York', '美国东部时间（自动夏令时）'], ['America/Los_Angeles', '美国西部时间（自动夏令时）'], ['UTC', '协调世界时（UTC）']];
const options = computed(() => choices.some(([value]) => value === props.modelValue)
  ? choices : [...choices, [props.modelValue, `当前地区：${props.modelValue}`]]);
</script>

<template>
  <mdui-select variant="outlined" :label="label" :value="modelValue" @change="emit('update:modelValue', readMduiValue($event))">
    <mdui-menu-item v-for="[value, text] in options" :key="value" :value="value">{{ text }}</mdui-menu-item>
  </mdui-select>
</template>
