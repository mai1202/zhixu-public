<script setup lang="ts">
import { ref } from 'vue';

/**
 * 整行可点击的 MDUI 涟漪表面。
 *
 * `<mdui-ripple>` 本身不监听事件，状态层与涟漪由宿主组件（mdui-button、mdui-list-item 等）
 * 通过 ripple-mixin 驱动。Vue 里没有对应 mixin，所以这里显式转发指针与焦点事件，
 * 让整行在悬浮、聚焦和按下时都有与按钮一致的反馈。
 */
const props = withDefaults(defineProps<{
  /** 读屏软件的整行说明，例如“查看详情：某事项”。 */
  label?: string;
  disabled?: boolean;
}>(), {
  label: '',
  disabled: false,
});

const emit = defineEmits<{ (event: 'activate'): void }>();

interface RippleElement extends HTMLElement {
  startPress?: (event?: Event) => void;
  startHover?: () => void;
  endHover?: () => void;
  startFocus?: () => void;
  endFocus?: () => void;
}

const ripple = ref<HTMLElement | null>(null);

function rippleElement(): RippleElement | null {
  return ripple.value as RippleElement | null;
}

function press(event: PointerEvent): void {
  if (props.disabled) return;
  // 自定义元素尚未升级时点击仍然有效，只是没有涟漪动画。
  rippleElement()?.startPress?.(event);
}

function hover(event: PointerEvent, entering: boolean): void {
  // 触摸屏的 pointerenter 不代表悬停，与 MDUI 内部判断保持一致。
  if (props.disabled || event.pointerType !== 'mouse') return;
  if (entering) rippleElement()?.startHover?.();
  else rippleElement()?.endHover?.();
}

function focus(focused: boolean): void {
  if (props.disabled) return;
  if (focused) rippleElement()?.startFocus?.();
  else rippleElement()?.endFocus?.();
}

function activate(): void {
  if (!props.disabled) emit('activate');
}
</script>

<template>
  <div
    class="ripple-surface"
    :class="{ 'ripple-surface--disabled': disabled }"
    role="link"
    :tabindex="disabled ? -1 : 0"
    :aria-label="label || undefined"
    :aria-disabled="disabled ? 'true' : undefined"
    @pointerdown="press"
    @pointerenter="hover($event, true)"
    @pointerleave="hover($event, false)"
    @focus="focus(true)"
    @blur="focus(false)"
    @click="activate"
    @keydown.enter.prevent="activate"
    @keydown.space.prevent="activate"
  >
    <slot></slot>
    <mdui-ripple ref="ripple"></mdui-ripple>
  </div>
</template>

<style scoped>
.ripple-surface {
  position: relative;
  overflow: hidden;
  cursor: pointer;
  -webkit-tap-highlight-color: transparent;
}

.ripple-surface--disabled {
  cursor: default;
}

.ripple-surface:focus-visible {
  outline: 2px solid var(--mdui-color-primary, #315da8);
  outline-offset: -2px;
}
</style>
