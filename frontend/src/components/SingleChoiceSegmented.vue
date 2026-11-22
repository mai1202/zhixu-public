<script setup lang="ts">
import { nextTick, ref } from 'vue';
import { readMduiValue } from '../utils/mdui';

const props = defineProps<{
  modelValue: string;
  ariaLabel?: string;
}>();

const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const group = ref<(HTMLElement & { value?: string }) | null>(null);

function onChange(event: Event): void {
  const value = readMduiValue(event);
  if (!value) {
    // MDUI 单选再次点击当前项会清空选择；这里恢复原值，保证始终有一个选中。
    void nextTick(() => {
      if (group.value) group.value.value = props.modelValue;
    });
    return;
  }
  if (value !== props.modelValue) emit('update:modelValue', value);
}
</script>

<template>
  <mdui-segmented-button-group
    ref="group"
    selects="single"
    :value="modelValue"
    :aria-label="ariaLabel"
    @change="onChange"
  >
    <slot></slot>
  </mdui-segmented-button-group>
</template>
