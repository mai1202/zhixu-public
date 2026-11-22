<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import type { TextField } from 'mdui/components/text-field.js';
import { useRoute, useRouter } from 'vue-router';
import { apiRequest, ApiError } from '../api/client';
import { useAuthStore } from '../stores/auth';

type RequestCodeResponse = { local_login: true } | { local_login?: false; challenge_id: string; message: string };


const route = useRoute();
const router = useRouter();
const auth = useAuthStore();
const email = ref('');
const privacy = ref<{ version: string; paragraphs: string[]; contact: string; password_login_enabled?: boolean } | null>(null);
const passwordMode = ref(false);
const password = ref('');
const privacyAccepted = ref(false);
onMounted(async () => {
  try {
    privacy.value = await apiRequest('/api/v1/privacy-notice');
    // 评委测试站直接展示密码入口；未开放测试密码的部署保留原验证码入口。
    passwordMode.value = Boolean(privacy.value?.password_login_enabled);
  }
  catch { error.value = '资料处理告知未加载，请刷新页面。'; }
});
// 本地开发页按测试后缀显示入口，服务端仍独立校验开发环境和回环来源。
const localTestEmail = computed(() => import.meta.env.DEV
  && ['127.0.0.1', 'localhost'].includes(window.location.hostname)
  && email.value.trim().toLowerCase().endsWith('@local.test'));
const emailField = ref<TextField | null>(null);
const code = ref('');
const challengeId = ref('');
const info = ref('');
const error = ref('');
const loading = ref(false);

function updateEmail(event: Event) {
  email.value = (event.target as HTMLInputElement).value;
  // 编辑时清除上一轮错误，输入未完成时不打断用户。
  emailField.value?.setCustomValidity('');
}

function validateEmail(): boolean {
  // 用原生邮箱规则校验格式，MDUI 只在离焦或提交时展示中文错误。
  const value = email.value.trim();
  const validator = document.createElement('input');
  validator.type = 'email';
  validator.required = true;
  validator.value = value;
  const message = !value ? (passwordMode.value ? '请输入测试账号邮箱。' : '请输入广外邮箱。')
    : validator.checkValidity() ? '' : '请输入完整的邮箱地址。';
  emailField.value?.setCustomValidity(message);
  return !message;
}

function updateCode(event: Event) {
  code.value = (event.target as HTMLInputElement).value.replace(/[^0-9]/g, '').slice(0, 8);
}

function errorMessage(value: unknown): string {
  return value instanceof ApiError ? value.message : '请求失败，请检查网络后重试。';
}

function handleAuthError(reason: unknown): void {
  // 白名单拒绝是配置事实，不是网络错误；只提示使用测试账号，不展示允许登录的地址。
  if (reason instanceof ApiError && reason.code === 'login_email_not_allowed') {
    error.value = '请登录测试账号。';
    return;
  }
  error.value = errorMessage(reason);
}

async function requestCode() {
  if (loading.value || !validateEmail()) return;
  if (!privacyAccepted.value || !privacy.value) { error.value = '请先阅读并同意资料处理告知。'; return; }
  error.value = '';
  info.value = '';
  loading.value = true;
  try {
    const response = await apiRequest<RequestCodeResponse>('/api/v1/auth/request-code', {
      method: 'POST',
      body: JSON.stringify({ email: email.value.trim(), policy_version: privacy.value.version }),
    });
    if (response.local_login) {
      // 通过同源 Cookie 恢复用户和 CSRF，复用正常登录后的路由跳转。
      await auth.refreshSession();
      auth.initialized = true;
      await goToTarget();
      return;
    }
    challengeId.value = response.challenge_id;
    info.value = response.message;
  } catch (reason) {
    handleAuthError(reason);
  } finally {
    loading.value = false;
  }
}

async function goToTarget() {
  // 仅允许站内路径，验证码和本地测试登录共用跳转规则。
  const returnTo = route.query.returnTo;
  const target = typeof returnTo === 'string' && returnTo.startsWith('/') && !returnTo.startsWith('//')
    ? returnTo : '/today';
  await router.replace(target + (route.hash.startsWith('#invite=') && target === '/matters/journeys' ? route.hash : ''));
}

async function verifyCode() {
  error.value = '';
  loading.value = true;
  try {
    await auth.verifyCode(challengeId.value, code.value);
    await goToTarget();
  } catch (reason) {
    handleAuthError(reason);
  } finally {
    loading.value = false;
  }
}

async function loginPassword() {
  if (loading.value || !validateEmail() || !privacy.value || !privacyAccepted.value) return;
  loading.value = true; error.value = '';
  try {
    await auth.verifyPassword(email.value.trim(), password.value, privacy.value.version);
    password.value = '';
    await goToTarget();
  } catch (reason) { handleAuthError(reason); }
  finally { loading.value = false; }
}

function switchLoginMode() {
  // 密码不保存在浏览器存储，切换登录方式时立即清空。
  passwordMode.value = !passwordMode.value;
  challengeId.value = ''; password.value = ''; code.value = ''; info.value = ''; error.value = '';
}
</script>

<template>
  <section class="login-page" aria-labelledby="login-heading">
    <div class="login-visual" aria-hidden="true">
      <div class="login-orbit login-orbit--one"></div>
      <div class="login-orbit login-orbit--two"></div>
      <div class="login-visual-copy">
        <span class="login-brand-mark">知</span>
        <p class="eyebrow">知序 · 校园信息管家</p>
        <h1 id="login-heading">把校园消息<br />变成下一步行动</h1>
        <p>通知、课表、待办和日程进入同一处，由持续对话的 Agent 帮你整理。</p>
      </div>
    </div>

    <div class="login-panel">
      <div class="login-panel-heading">
        <span class="login-step-chip">{{ passwordMode ? '测试账号' : challengeId ? '第 2 步 / 2' : '第 1 步 / 2' }}</span>
        <h2>{{ challengeId ? '输入验证码' : '登录知序' }}</h2>
        <p>{{ passwordMode ? '使用管理员提供的邮箱和密码登录测试账号。' : challengeId ? `${email} 收到的 8 位验证码将在 10 分钟内有效。` : '使用广东外语外贸大学邮箱验证身份，不需要另外设置密码。' }}</p>
      </div>

      <form v-if="!challengeId" class="login-form" @submit.prevent="passwordMode ? loginPassword() : requestCode()">
        <mdui-text-field
          ref="emailField"
          :label="passwordMode ? '测试账号邮箱' : '广外邮箱'"
          type="text"
          inputmode="email"
          autocomplete="email"
          maxlength="254"
          aria-required="true"
          :value="email"
          @input="updateEmail"
          @blur="validateEmail"
        ></mdui-text-field>
        <mdui-text-field v-if="passwordMode" label="密码" type="password" autocomplete="current-password"
          :value="password" @input="password = ($event.target as HTMLInputElement).value"></mdui-text-field>
        <details v-if="privacy" class="privacy-notice"><summary>资料处理告知</summary><p v-for="paragraph in privacy.paragraphs" :key="paragraph">{{ paragraph }}</p><p>{{ privacy.contact }}</p></details>
        <label><input v-model="privacyAccepted" type="checkbox" />我已阅读并同意资料处理告知</label>
        <mdui-button type="submit" variant="filled" :disabled="loading || !privacyAccepted || !privacy">
          {{ passwordMode ? (loading ? '正在登录' : '登录测试账号') : loading ? (localTestEmail ? '正在登录' : '正在发送') : (localTestEmail ? '本地测试登录' : '发送验证码') }}
        </mdui-button>
        <p class="login-trust">邮箱只用于登录验证与通知投递，不会公开给其他用户。</p>
      </form>

      <form v-else class="login-form" @submit.prevent="verifyCode">
        <mdui-text-field
          label="8 位验证码"
          type="text"
          inputmode="numeric"
          autocomplete="one-time-code"
          maxlength="8"
          required
          :value="code"
          @input="updateCode"
        ></mdui-text-field>
        <mdui-button type="submit" variant="filled" :disabled="loading || code.length !== 8">
          {{ loading ? '正在验证' : '验证并进入' }}
        </mdui-button>
        <div class="login-actions">
          <mdui-button type="button" variant="text" :disabled="loading" @click="requestCode">重新发送</mdui-button>
          <mdui-button type="button" variant="text" :disabled="loading" @click="challengeId = ''; code = ''; info = ''">
            更换邮箱
          </mdui-button>
        </div>
      </form>

      <mdui-button v-if="privacy?.password_login_enabled" type="button" variant="text" :disabled="loading" @click="switchLoginMode">
        {{ passwordMode ? '使用邮箱验证码登录' : '使用测试账号密码登录' }}
      </mdui-button>
      <p v-if="info" class="notice-text login-inline-message" role="status">{{ info }}</p>
      <p v-if="error" class="error-text login-inline-message" role="alert">{{ error }}</p>

      <div class="login-benefits" aria-label="产品能力">
        <span>通知自动提取</span>
        <span>课程真实时间轴</span>
        <span>跨对话记忆</span>
      </div>
    </div>
  </section>
</template>
