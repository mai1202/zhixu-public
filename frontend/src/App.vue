<script setup lang="ts">
import { computed } from 'vue';
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router';
import AppIcon from './components/AppIcon.vue';
import MainAgentChat from './components/MainAgentChat.vue';
import { useAuthStore } from './stores/auth';

const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const pageTitle = computed(() => String(route.meta.title ?? '知序'));
// 日程页要按视口高度铺满日历，单独标记主内容区以便使用对应的高度约束。
const isSchedulePage = computed(() => route.path === '/courses' || route.path === '/schedule');

// 主导航固定为五个任务入口，低频配置收进“设置”，避免移动端底部导航拥挤。
const navigation = [
  { label: '今天', href: '/today', icon: 'today', activeIcon: 'today' },
  { label: '日程', href: '/courses', icon: 'calendar_month', activeIcon: 'calendar_month' },
  { label: '事务', href: '/matters', icon: 'checklist', activeIcon: 'checklist' },
  { label: '通知', href: '/inbox', icon: 'notifications', activeIcon: 'notifications_active' },
  { label: '助手', href: '/agent', icon: 'smart_toy', activeIcon: 'smart_toy' },
];

const activeNavigation = computed(() => {
  const path = route.path;
  if (path.startsWith('/courses') || path.startsWith('/schedule')) return '/courses';
  if (path.startsWith('/matters') || path.startsWith('/tasks/')) return '/matters';
  if (path.startsWith('/agent') || path.startsWith('/activity')) return '/agent';
  if (path.startsWith('/inbox') || path.startsWith('/notifications')) return '/inbox';
  if (path.startsWith('/settings') || path.startsWith('/mail') || path.startsWith('/profile')) return '';
  return '/today';
});

function navigateFromItem(path: string, event: MouseEvent): void {
  // 只响应真实点击；程序化更新选中值也会触发 change，不能用于跳转。
  event.preventDefault();
  if (path !== route.path) void router.push(path);
}
</script>

<template>
  <div class="app-shell" :class="{ 'app-shell--guest': !auth.user }">
    <template v-if="auth.user">
      <header class="app-top-bar">
        <div class="app-top-leading">
          <!-- 品牌仅保留文字，字重为 500，不显示图形 Logo。 -->
          <RouterLink class="app-brand app-brand-text" to="/today" aria-label="知序首页">知序</RouterLink>
          <span class="app-page-title">{{ pageTitle }}</span>
        </div>
        <a
          v-if="route.path === '/schedule' || route.path === '/courses'"
          class="app-calendar-export"
          href="/api/v1/schedule/export.ics"
          download
          aria-label="导出日历"
        >
          <AppIcon name="calendar_month"></AppIcon>
          <span>导出日历</span>
        </a>
        <!-- 设置保留纯图标，单一链接承担点击和键盘焦点，避免嵌套按钮。 -->
        <RouterLink class="app-profile-link" to="/settings" aria-label="打开设置" title="设置">
          <AppIcon name="settings"></AppIcon>
        </RouterLink>
      </header>

      <div class="app-body">
        <mdui-navigation-rail class="desktop-navigation" :value="activeNavigation" aria-label="主导航">
          <mdui-navigation-rail-item
            v-for="item in navigation"
            :key="item.href"
            :value="item.href"
            :href="item.href"
            @click="navigateFromItem(item.href, $event)"
          >
            <AppIcon slot="icon" :name="item.icon"></AppIcon>
            <AppIcon slot="active-icon" :name="item.activeIcon"></AppIcon>
            {{ item.label }}
          </mdui-navigation-rail-item>
        </mdui-navigation-rail>

        <main
          class="main-content"
          :class="{
            'main-content--chat': route.path === '/agent',
            'main-content--schedule': isSchedulePage,
          }"
        >
          <RouterView />
        </main>
      </div>

      <mdui-navigation-bar class="mobile-navigation" :value="activeNavigation" label-visibility="labeled" aria-label="主导航">
        <mdui-navigation-bar-item
          v-for="item in navigation"
          :key="item.href"
          :value="item.href"
          :href="item.href"
          @click="navigateFromItem(item.href, $event)"
        >
          <AppIcon slot="icon" :name="item.icon"></AppIcon>
          <AppIcon slot="active-icon" :name="item.activeIcon"></AppIcon>
          {{ item.label }}
        </mdui-navigation-bar-item>
      </mdui-navigation-bar>
      <MainAgentChat v-if="route.path !== '/agent'"
        :hide-launcher="route.path === '/settings'"
        :context="`${pageTitle}；页面地址：${route.fullPath}`" />
    </template>

    <main v-else class="login-shell">
      <RouterView />
    </main>
  </div>
</template>
