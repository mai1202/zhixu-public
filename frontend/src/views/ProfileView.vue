<script setup lang="ts">
import { ref } from 'vue';
import { useRouter } from 'vue-router';
import { ApiError } from '../api/client';
import AppIcon from '../components/AppIcon.vue';
import { useAuthStore } from '../stores/auth';

const router = useRouter();
const auth = useAuthStore();
const errorMessage = ref('');
const loggingOut = ref(false);

async function logout(): Promise<void> {
  errorMessage.value = '';
  loggingOut.value = true;
  try {
    await auth.logout();
    await router.replace('/login');
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '退出登录失败，请重试。';
  } finally {
    loggingOut.value = false;
  }
}
</script>

<template>
  <section class="page-section profile-page" aria-labelledby="profile-heading">
    <RouterLink class="app-link" to="/settings">← 返回设置目录</RouterLink>
    <header class="profile-hero">
      <div class="profile-avatar" aria-hidden="true">{{ auth.user?.email.slice(0, 1).toUpperCase() }}</div>
      <div>
        <p class="eyebrow">设置 / 账户</p>
        <h1 id="profile-heading">{{ auth.user?.email }}</h1>
        <p>把通知、课表和日程交给同一个助手持续整理。</p>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>

    <section class="profile-group" aria-labelledby="profile-account">
      <h2 id="profile-account">账户</h2>
      <mdui-list>
        <mdui-list-item
          :headline="loggingOut ? '正在退出……' : '退出登录'"
          description="本机保存的对话和日程不会被删除"
          :disabled="loggingOut"
          @click="logout"
        >
          <AppIcon slot="icon" name="logout"></AppIcon>
        </mdui-list-item>
      </mdui-list>
    </section>

    <p class="profile-version">知序 · 校园通知与日程助手</p>
  </section>
</template>
