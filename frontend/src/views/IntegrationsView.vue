<script setup lang="ts">
/** 凭证只保存在当前页面内存，刷新或离开后不再显示。 */
import { onMounted, ref } from 'vue';
import { apiRequest, ApiError } from '../api/client';
interface Feed { id: string; title: string; url: string; error_code: string | null; last_success_epoch: number | null }
interface MailIngress { recipient: string | null; configured: boolean; command_enabled: boolean; command_subject_tag: string | null }
const feeds = ref<Feed[]>([]);
const token = ref('');
const enabled = ref(false);
const mail = ref<MailIngress | null>(null);
const title = ref('');
const url = ref('');
const error = ref('');
const busy = ref(false);
const endpoint = `${window.location.origin}/api/v1/integrations/webhook`;
async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { await action(); } catch (failure) { error.value = failure instanceof ApiError ? failure.message : '操作失败，请重试。'; }
  finally { busy.value = false; }
}
onMounted(() => void run(async () => {
  const [hook, result, inbox] = await Promise.all([apiRequest<{ enabled: boolean }>('/api/v1/integrations/webhook'), apiRequest<{ items: Feed[] }>('/api/v1/integrations/feeds'), apiRequest<MailIngress>('/api/v1/mail-ingress')]);
  enabled.value = hook.enabled; feeds.value = result.items; mail.value = inbox;
}));
function issue(): void { void run(async () => { const result = await apiRequest<{ token: string }>('/api/v1/integrations/webhook/token', { method: 'POST', body: '{}' }); token.value = result.token; enabled.value = true; }); }
function revoke(): void { void run(async () => { await apiRequest('/api/v1/integrations/webhook/token', { method: 'DELETE' }); token.value = ''; enabled.value = false; }); }
function add(): void { void run(async () => {
  feeds.value = (await apiRequest<{ items: Feed[] }>('/api/v1/integrations/feeds', { method: 'POST', body: JSON.stringify({ title: title.value, url: url.value }) })).items;
  title.value = ''; url.value = '';
}); }
function remove(id: string): void { void run(async () => { feeds.value = (await apiRequest<{ items: Feed[] }>(`/api/v1/integrations/feeds/${id}`, { method: 'DELETE' })).items; }); }
function commands(enabled: boolean): void { void run(async () => { mail.value = await apiRequest<MailIngress>('/api/v1/mail-ingress/commands', { method: 'PUT', body: JSON.stringify({ enabled }) }); }); }
</script>
<template>
  <section class="page-section integration-page"><h1>通知接入</h1><p v-if="error" role="alert" class="error-text">{{ error }}</p>
    <section><h2>Webhook</h2><p>让自己的程序将通知投递到知序。此凭证只能投递，不能读取你的资料。</p>
      <p>地址：<code>{{ endpoint }}</code></p><p>状态：{{ enabled ? '已启用' : '未启用' }}</p>
      <mdui-button :disabled="busy" @click="issue">{{ enabled ? '轮换凭证' : '创建凭证' }}</mdui-button>
      <mdui-button v-if="enabled" :disabled="busy" variant="text" @click="revoke">撤销凭证</mdui-button>
      <label v-if="token">投递凭证（仅本次显示）<input :value="token" readonly autocomplete="off" /></label>
      <p>使用 POST 请求，Authorization 头为 Bearer 加凭证，Content-Type 为 application/json。相同 source_key 重试不会重复建通知；同一键的内容不能改变。</p>
      <pre>{ "source_key": "notice-001", "title": "报名提醒", "text": "请在12月1日前提交报名表。" }</pre>
    </section>
    <section v-if="mail"><h2>通过邮件操作助手</h2><p>使用独立的命令码，将本人写下的操作要求发到运营邮箱。普通收件码和手动导入的 .eml 始终只作为资料。</p>
      <p>收件地址：{{ mail.recipient || '尚未配置' }} · {{ mail.configured ? '已配置收信' : '尚未配置真实收信，暂不能通过邮件操作' }}</p>
      <mdui-button :disabled="busy" @click="commands(!mail.command_enabled)">{{ mail.command_enabled ? '关闭邮件操作并撤销命令码' : '单独启用邮件操作' }}</mdui-button>
      <template v-if="mail.command_enabled"><p>从本人已验证邮箱发送，在主题中加入以下命令码，正文填写操作要求。命令码可发起本人助手操作，请勿用于群发或转发邮件。</p><code>{{ mail.command_subject_tag }}</code>
        <p>结果在主助手中查看；如需邮件回复，请在正文中明确说明。发送邮件和修改设置仍需独立工具审查。</p></template>
    </section>
    <section><h2>公众号与网站订阅源</h2><p>填写发布方提供或你有权使用的公开 HTTPS RSS/Atom 地址。后台约每30分钟检查更新，新增内容进入通知。</p>
      <p>微信公众号须有可用订阅源才能自动跟踪；普通公众号主页不能直接替代订阅源。单篇文章也可复制正文、转发邮件或上传截图。</p>
      <form @submit.prevent="add"><label>名称<input v-model="title" required maxlength="200" /></label><label>订阅地址<input v-model="url" type="url" required maxlength="2000" placeholder="https://…/feed.xml" /></label><mdui-button type="submit" :disabled="busy">订阅</mdui-button></form>
      <ul><li v-for="feed in feeds" :key="feed.id"><strong>{{ feed.title }}</strong><p>{{ feed.url }}</p>
        <p>{{ feed.error_code ? `上次检查未成功：${feed.error_code}` : feed.last_success_epoch ? `上次成功：${new Date(feed.last_success_epoch * 1000).toLocaleString('zh-CN')}` : '等待首次检查' }}</p>
        <mdui-button variant="text" :disabled="busy" @click="remove(feed.id)">取消订阅</mdui-button></li></ul>
    </section>
  </section>
</template>
<style scoped>
.integration-page{max-width:860px}.integration-page section{margin:24px 0;padding:22px;border:1px solid #ddd;border-radius:18px;overflow-wrap:anywhere}label{display:block;margin:14px 0}input{display:block;box-sizing:border-box;width:100%;padding:10px;border:1px solid #aaa;border-radius:8px;font:inherit;background:transparent;color:inherit}pre{white-space:pre-wrap}li{margin:16px 0}
</style>
