<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { getModelBilling, listProviderProfiles, updateProviderProfile, type ModelBilling, type ProviderProfile } from '../../api/models';
import { ApiError } from '../../api/client';
import { formatRelativeDateTime } from '../../utils/datetime';

const data = ref<ModelBilling | null>(null);
const profiles = ref<ProviderProfile[]>([]);
const selected = ref('');
const error = ref('');
const loading = ref(false);
const rates = ref(['', '', '']);
let timer: number | undefined;
let disposed = false;
const profile = computed(() => profiles.value.find(item => item.id === selected.value));
const models = computed(() => data.value?.models.filter(item => !selected.value || item.profile_id === selected.value) ?? []);
const recent = computed(() => data.value?.recent.filter(item => !selected.value || item.profile_id === selected.value) ?? []);
// 仅刷新账单，不触发模型调用；离开页面后不再更新组件。
async function refresh() {
  if (loading.value) return;
  loading.value = true;
  try {
    const [bill, configs] = await Promise.all([getModelBilling(), listProviderProfiles()]);
    if (disposed) return;
    data.value = bill; profiles.value = configs.items; error.value = '';
  } catch (failure) { if (!disposed) error.value = failure instanceof ApiError ? failure.message : '计费记录加载失败，请重试。'; }
  finally { loading.value = false; }
}
function selectProfile() {
  const prices = profile.value?.billing_prices;
  rates.value = prices ? [prices.cache_hit, prices.cache_miss, prices.output] : ['', '', ''];
}
async function savePrices() {
  if (!profile.value || !rates.value.every(value => value.trim() && Number.isFinite(Number(value)) && Number(value) >= 0)) return;
  try {
    await updateProviderProfile(profile.value, { billing_prices: { cache_hit: rates.value[0]!, cache_miss: rates.value[1]!, output: rates.value[2]! } });
    await refresh();
  } catch (failure) { error.value = failure instanceof ApiError ? failure.message : '单价保存失败。'; }
}
function money(value: number) { return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', minimumFractionDigits: 4, maximumFractionDigits: 6 }).format(value); }
onMounted(() => { void refresh(); timer = window.setInterval(() => { if (document.visibilityState === 'visible') void refresh(); }, 15000); });
onUnmounted(() => { disposed = true; window.clearInterval(timer); });
</script>

<template>
  <section class="model-billing" aria-label="模型计费">
    <header><div><h2>模型计费</h2><p>今日 · 北京时间 {{ data?.quota.day ?? '加载中' }}</p></div><button type="button" :disabled="loading" @click="refresh">刷新账单</button></header>
    <p v-if="error" class="error-text" role="alert">{{ error }}</p>
    <template v-if="data">
      <div class="billing-totals">
        <div><span>平台已计费</span><strong>{{ money(data.quota.spent_cny) }}</strong></div>
        <div><span>调用中预留</span><strong>{{ money(data.quota.reserved_cny) }}</strong></div>
        <div><span>{{ data.quota.exempt ? '账户额度' : `今日可用 / ¥${data.quota.limit_cny}` }}</span><strong>{{ data.quota.exempt ? '豁免限制' : money(data.quota.remaining_cny ?? 0) }}</strong></div>
      </div>
      <p v-if="!data.quota.exempt && (data.quota.remaining_cny ?? 0) <= 0" class="error-text">今日预算已用完或已预留，请等待北京时间零点重置。</p>
      <p class="helper-text">每天零点重置。{{ data.quota.self_configured_excluded ? '自有密钥不占平台额度。' : '此测试账号的云端模型调用均受日预算限制。' }}预留按高峰价格及输入上界计算，结算后释放差额。</p>
      <label class="billing-filter">查看模型<select v-model="selected" @change="selectProfile"><option value="">全部模型</option><option v-for="item in profiles" :key="item.id" :value="item.id">{{ item.label }} · {{ item.model_id }}</option></select></label>
      <div class="billing-models">
        <article v-for="item in models" :key="`${item.profile_id}-${item.model_id}-${item.platform_paid}`">
          <h3>{{ item.label }}</h3><p>{{ item.model_id }} · {{ item.platform_paid ? '平台付费' : '自有密钥' }}</p>
          <strong>{{ item.unknown_calls ? '已知部分 ' : '' }}{{ money(item.spent_cny) }}</strong><span> · {{ item.calls }} 次请求</span>
          <p>输入 {{ item.prompt_tokens.toLocaleString() }}（缓存 {{ item.cache_hit_tokens.toLocaleString() }} / 未缓存 {{ item.cache_miss_tokens.toLocaleString() }}）<br />输出 {{ item.completion_tokens.toLocaleString() }} tokens（含模型思考）</p>
          <p v-if="item.reserved_cny">调用中预留 {{ money(item.reserved_cny) }}</p>
          <p v-if="item.unknown_calls" class="error-text">{{ item.unknown_calls }} 次请求单价未知，未计入已知金额。选择此配置可填写参考单价；历史账单保留原始状态。</p>
          <p v-if="item.estimated_calls" class="helper-text">{{ item.estimated_calls }} 次保守估算（跨高峰、回执缺失、假期表待更新或升级前历史）。</p>
          <details><summary>最近请求的单价快照</summary><p v-if="item.pricing.rates">每百万 tokens：缓存 ¥{{ item.pricing.rates[0] }} / 未缓存 ¥{{ item.pricing.rates[1] }} / 输出 ¥{{ item.pricing.rates[2] }}；{{ item.pricing.period === 'peak' ? '高峰' : item.pricing.period === 'custom' ? '自填参考价' : '空闲' }}时段。</p><p v-else>没有可核验的单价快照。</p><p v-if="item.pricing.calendar_estimated">该年份假期表尚未录入，工作日高峰价格为保守估算。</p></details>
        </article>
      </div>
      <p v-if="!models.length" class="helper-text">今日尚无此模型的云端调用。账单从调用开始记录，本地模型不产生云端模型费。</p>
      <form v-if="profile && !profile.is_default && profile.provider !== 'lm_studio'" class="billing-price-form" @submit.prevent="savePrices">
        <h3>自有模型参考单价（元 / 百万 tokens）</h3><p>DeepSeek 官方端点自动按官方价计算；其他云端模型可按自己的服务商账单填写，后续请求使用新单价。</p>
        <label v-for="(title, index) in ['缓存输入', '未缓存输入', '输出']" :key="title">{{ title }}<input v-model="rates[index]" type="number" min="0" max="100000" step="0.000001" required /></label><button type="submit">保存参考单价</button>
      </form>
      <details v-if="recent.length"><summary>最近调用明细</summary><ul><li v-for="item in recent" :key="item.id">{{ formatRelativeDateTime(item.created_at_epoch) }} · {{ item.model_id }} · {{ item.status === 'reserved' ? '调用中' : item.cost_cny === null ? '单价未知' : `${money(item.cost_cny)}${item.status === 'estimated' ? '（上界估算）' : ''}` }}</li></ul></details>
      <p class="helper-text">{{ data.note }}</p><a class="app-link" :href="data.pricing_source" target="_blank" rel="noopener noreferrer">DeepSeek 官方价格</a> · <RouterLink class="app-link" to="/settings/models">绑定自己的模型 / 连接设置</RouterLink>
    </template>
  </section>
</template>
<style scoped>
/* 账单采用可换行卡片，在手机上也能完整阅读输入、输出与费用。 */
.model-billing{margin:24px 0;padding:24px;border:1px solid var(--app-outline,#dfe3ec);border-radius:18px;background:var(--app-surface,#fff);line-height:1.7}.model-billing header{display:flex;justify-content:space-between;align-items:center;gap:12px}.model-billing h2,.model-billing h3{margin:0}.model-billing p{margin:8px 0}.model-billing button,.model-billing select,.model-billing input{font:inherit;min-height:44px;padding:8px 12px;border:1px solid #c5cddd;border-radius:8px;background:var(--app-surface,#fff);color:inherit;max-width:100%;box-sizing:border-box}.model-billing button{cursor:pointer;color:var(--app-primary,#315da8)}.billing-totals{display:flex;gap:24px;flex-wrap:wrap;margin:20px 0}.billing-totals span,.billing-totals strong{display:block}.billing-totals strong{font-size:22px}.billing-filter{display:grid;gap:8px;max-width:100%}.billing-models{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,290px),1fr));gap:16px;margin:20px 0}.billing-models article{padding:16px;border-radius:12px;background:var(--app-surface-container,#f0f3fb);overflow-wrap:anywhere}.model-billing details{margin:12px 0}.model-billing summary{cursor:pointer}.billing-price-form{display:flex;flex-wrap:wrap;gap:12px;margin:20px 0}.billing-price-form h3,.billing-price-form p{width:100%}.billing-price-form label{display:grid;gap:6px;max-width:100%}@media(max-width:600px){.model-billing{padding:16px}.billing-totals{gap:16px}.billing-totals strong{font-size:19px}}
</style>
