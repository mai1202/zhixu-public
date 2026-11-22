<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import AppIcon from '../components/AppIcon.vue';
import { readMduiValue } from '../utils/mdui';
import { ApiError } from '../api/client';
import {
  archiveKnowledgeDocument,
  getKnowledgeStatus,
  importEml,
  importWebPage,
  readKnowledgeEntry,
  rotateMailIngressToken,
  saveKnowledgeDocument,
  searchKnowledge,
  type KnowledgeDetail,
  type KnowledgeItem,
  type KnowledgeSearchResult,
  type KnowledgeStatus,
  type MailIngressStatus,
  type KnowledgeKind,
  type SearchMode,
} from '../api/knowledge';

const route = useRoute();
const router = useRouter();
const query = ref(typeof route.query.q === 'string' ? route.query.q : '');
const mode = ref<SearchMode>((typeof route.query.mode === 'string' ? route.query.mode : 'auto') as SearchMode);
const scope = ref<'all' | 'private' | 'public'>(typeof route.query.scope === 'string' ? route.query.scope as 'all' | 'private' | 'public' : 'all');
const kind = ref(typeof route.query.kind === 'string' ? route.query.kind : 'all');
const result = ref<KnowledgeSearchResult | null>(null);
const status = ref<KnowledgeStatus | null>(null);
const detail = ref<KnowledgeDetail | null>(null);
const detailBusy = ref(false);
const searching = ref(false);
const saving = ref(false);
const mailBusy = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
const note = ref({
  title: '', content: '', scope: 'private' as 'private' | 'public',
  publisher: '', published_date: '', effective_status: 'unknown' as 'effective' | 'expired' | 'unknown',
  audience: '',
});
const showNoteForm = ref(false);
const webUrl = ref('');
const webBusy = ref(false);
const mail = ref<MailIngressStatus | null>(null);
const emlInput = ref<HTMLInputElement | null>(null);

const canSearch = computed(() => Boolean(query.value.trim()) && !searching.value);
const retrievalLabel = computed(() => {
  const retrieval = status.value?.search.retrieval;
  if (!retrieval) return '关键词';
  return retrieval.method === 'hybrid' ? '关键词 + 向量' : '关键词';
});

function fail(error: unknown, fallback: string): void {
  errorMessage.value = error instanceof ApiError ? error.message : fallback;
  successMessage.value = '';
}

function clearMessages(): void {
  errorMessage.value = '';
  successMessage.value = '';
}

async function refreshStatus(): Promise<void> {
  status.value = await getKnowledgeStatus();
  mail.value = status.value.mail_ingress;
}

async function runSearch(refresh = false): Promise<void> {
  if (!canSearch.value) return;
  clearMessages();
  searching.value = true;
  try {
    result.value = await searchKnowledge(query.value.trim(), {
      mode: mode.value, scope: scope.value, kind: kind.value as KnowledgeKind, limit: 10,
      web: mode.value === 'local' ? 'never' : 'auto', refresh,
    });
  } catch (error) { fail(error, '检索暂时失败，请稍后再试。'); }
  finally { searching.value = false; }
}

async function openEntry(item: KnowledgeItem): Promise<void> {
  detailBusy.value = true;
  clearMessages();
  try {
    detail.value = await readKnowledgeEntry(item.kind, item.id, 0);
  } catch (error) { fail(error, '无法读取这条资料的原文。'); }
  finally { detailBusy.value = false; }
}

async function openDeepLink(): Promise<void> {
  const id = route.query.document;
  if (typeof id !== 'string' || !id) return;
  detailBusy.value = true;
  try {
    detail.value = await readKnowledgeEntry('document', id, 0);
  } catch (error) { fail(error, '无法打开该资料。'); }
  finally { detailBusy.value = false; }
}

async function readMore(): Promise<void> {
  if (!detail.value?.next_offset) return;
  const current = detail.value;
  detailBusy.value = true;
  try {
    const next = await readKnowledgeEntry(current.kind, current.id, current.next_offset ?? 0);
    detail.value = { ...next, content: current.content + '\n' + next.content, offset: 0 };
  } catch (error) { fail(error, '继续读取失败。'); }
  finally { detailBusy.value = false; }
}

async function saveNote(): Promise<void> {
  if (!note.value.title.trim() || !note.value.content.trim()) {
    errorMessage.value = '请填写标题和内容。';
    return;
  }
  saving.value = true;
  clearMessages();
  try {
    await saveKnowledgeDocument({
      title: note.value.title, content: note.value.content, scope: note.value.scope,
      publisher: note.value.publisher.trim() || null,
      published_date: note.value.published_date || null,
      effective_status: note.value.effective_status,
      audience: note.value.audience.trim() || null,
    });
    note.value = { title: '', content: '', scope: 'private', publisher: '', published_date: '', effective_status: 'unknown', audience: '' };
    showNoteForm.value = false;
    successMessage.value = '资料已保存，可在本页检索并回查原文。';
    await refreshStatus();
    if (query.value.trim()) await runSearch(true);
  } catch (error) { fail(error, '保存资料失败。'); }
  finally { saving.value = false; }
}

async function archiveCurrent(): Promise<void> {
  if (!detail.value) return;
  if (!window.confirm('归档后默认检索不再返回这份资料，原文仍保留。继续吗？')) return;
  clearMessages();
  try {
    await archiveKnowledgeDocument(detail.value.id, detail.value.version);
    successMessage.value = '资料已归档，可重新保存恢复。';
    detail.value = null;
    await refreshStatus();
    if (query.value.trim()) await runSearch(true);
  } catch (error) { fail(error, '归档失败。'); }
}

async function readWebPage(): Promise<void> {
  const value = webUrl.value.trim();
  if (!/^https:\/\//.test(value)) {
    errorMessage.value = '学校网页读取只接受公开 HTTPS 地址。';
    return;
  }
  webBusy.value = true;
  clearMessages();
  try {
    const page = await importWebPage(value, 'private');
    webUrl.value = '';
    successMessage.value = '网页已保存为私人快照，可检索并回查抓取时间。';
    detail.value = page;
    await refreshStatus();
  } catch (error) { fail(error, '网页读取失败；请核对地址或稍后重试。'); }
  finally { webBusy.value = false; }
}

async function rotateToken(): Promise<void> {
  if (!window.confirm('轮换后旧收件码立即失效，需要把新码放进后续转发邮件的主题。继续吗？')) return;
  mailBusy.value = true;
  clearMessages();
  try {
    mail.value = await rotateMailIngressToken();
    successMessage.value = '收件码已轮换，请只在自己可见的位置保存。';
    await refreshStatus();
  } catch (error) { fail(error, '轮换收件码失败。'); }
  finally { mailBusy.value = false; }
}

async function uploadEml(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  mailBusy.value = true;
  clearMessages();
  try {
    const imported = await importEml(file);
    successMessage.value = '邮件已导入为通知资料' + (imported.attachments.length ? `，含 ${imported.attachments.length} 个附件。` : '。');
    await refreshStatus();
    if (query.value.trim()) await runSearch(true);
  } catch (error) { fail(error, '邮件导入失败；请检查 .eml 文件是否完整。'); }
  finally {
    mailBusy.value = false;
    input.value = '';
  }
}

function effectiveLabel(value: string | null | undefined): string {
  return value === 'effective' ? '现行有效' : value === 'expired' ? '已失效' : '有效性未确认';
}

function scopeLabel(value: string): string {
  return value === 'public' ? '公共库' : value === 'private' ? '私人库' : '全部资料';
}

function kindLabel(value: string): string {
  const labels: Record<string, string> = {
    document: '文档', notification: '通知', memory: '长期记忆', conversation: '对话', attachment: '附件', web: '网页',
  };
  return labels[value] ?? value;
}

function channelLabel(value: string): string {
  const labels: Record<string, string> = { local: '本地', ddg: 'DuckDuckGo', brave: 'Brave', web: '网页' };
  return labels[value] ?? value;
}

function channelState(value: string | undefined): string {
  const labels: Record<string, string> = {
    ok: '可用', empty: '无结果', failed: '失败', skipped: '未请求', not_configured: '未配置', circuit_open: '熔断', disabled: '已关闭',
  };
  return labels[value ?? ''] ?? value ?? '未知';
}

function externalUrl(item: KnowledgeItem | KnowledgeDetail): string | null {
  const candidate = 'source_url' in item ? item.source_url : null;
  return candidate && candidate.startsWith('https://') ? candidate : null;
}

async function updateUrl(): Promise<void> {
  const next: Record<string, string> = {};
  if (query.value.trim()) next.q = query.value.trim();
  if (mode.value !== 'auto') next.mode = mode.value;
  if (scope.value !== 'all') next.scope = scope.value;
  if (kind.value !== 'all') next.kind = kind.value;
  await router.replace({ path: '/knowledge', query: next });
}

async function submitSearch(): Promise<void> {
  await updateUrl();
  await runSearch();
}

onMounted(async () => {
  try {
    await refreshStatus();
    if (query.value.trim()) await runSearch();
    await openDeepLink();
  } catch (error) { fail(error, '知识库状态暂时无法加载。'); }
});
</script>

<template>
  <section class="page-section knowledge-page" aria-labelledby="knowledge-heading">
    <header class="knowledge-hero">
      <div>
        <p class="eyebrow">有依据地找回与核对</p>
        <h1 id="knowledge-heading">知识库</h1>
        <p>联合检索本人通知、记忆、对话、附件和公共规定，查看引用出处；需要最新信息时可查 DuckDuckGo，配置 Brave 后参与回退或核对。</p>
      </div>
      <div class="knowledge-status" aria-label="检索方式">
        <span class="knowledge-badge"><AppIcon name="search"></AppIcon>{{ retrievalLabel }}</span>
        <span class="knowledge-badge" :data-state="status?.search.duckduckgo.status"><AppIcon name="hub"></AppIcon>DDG {{ channelState(status?.search.duckduckgo.status) }}</span>
        <span class="knowledge-badge" :data-state="status?.search.brave.status"><AppIcon name="shield"></AppIcon>Brave {{ channelState(status?.search.brave.status) }}</span>
        <span class="knowledge-badge"><AppIcon name="lock"></AppIcon>私人 {{ status?.documents.private ?? 0 }} · 公共 {{ status?.documents.public ?? 0 }}</span>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <form class="knowledge-search" role="search" @submit.prevent="submitSearch">
      <mdui-text-field class="knowledge-search-input" label="要找回什么？" type="search" :value="query" maxlength="300" placeholder="例如：上次那个报名什么时候截止" @input="query = readMduiValue($event)"></mdui-text-field>
      <mdui-select variant="outlined" label="模式" :value="mode" @change="mode = readMduiValue($event) as SearchMode">
        <mdui-menu-item value="auto">自动（必要时联网）</mdui-menu-item>
        <mdui-menu-item value="local">仅查已有资料</mdui-menu-item>
        <mdui-menu-item value="multi">多源核对</mdui-menu-item>
      </mdui-select>
      <mdui-select variant="outlined" label="范围" :value="scope" @change="scope = readMduiValue($event) as 'all' | 'private' | 'public'">
        <mdui-menu-item value="all">全部资料</mdui-menu-item>
        <mdui-menu-item value="private">私人库</mdui-menu-item>
        <mdui-menu-item value="public">公共库</mdui-menu-item>
      </mdui-select>
      <mdui-select variant="outlined" label="类型" :value="kind" @change="kind = readMduiValue($event)">
        <mdui-menu-item value="all">全部</mdui-menu-item>
        <mdui-menu-item value="document">文档</mdui-menu-item>
        <mdui-menu-item value="notification">通知</mdui-menu-item>
        <mdui-menu-item value="attachment">附件</mdui-menu-item>
        <mdui-menu-item value="memory">长期记忆</mdui-menu-item>
        <mdui-menu-item value="conversation">对话</mdui-menu-item>
      </mdui-select>
      <mdui-button type="submit" variant="filled" :disabled="!canSearch">
        <AppIcon name="search"></AppIcon>{{ searching ? '检索中……' : '检索' }}
      </mdui-button>
    </form>

    <p v-if="result" class="knowledge-evidence-note">
      {{ result.evidence_note }}
      <span v-if="result.cached">本结果来自短时缓存。</span>
      <span>耗时 {{ result.elapsed_ms }} ms；{{ result.method === 'hybrid' ? '混合检索' : '关键词检索' }}。</span>
    </p>

    <section v-if="result" class="knowledge-channels" aria-label="检索渠道状态">
      <span v-for="(channel, name) in result.channels" :key="name" :data-state="channel.status">
        {{ channelLabel(String(name)) }}：{{ channelState(channel.status) }}（{{ channel.count }}）
        <small v-if="channel.message">{{ channel.message }}</small>
      </span>
    </section>

    <div class="knowledge-layout">
      <section class="knowledge-results" aria-live="polite">
        <h2>检索结果</h2>
        <p v-if="!result" class="helper-text">输入关键词开始检索。没有联网结果时，已有资料仍可查询。</p>
        <p v-else-if="!result.items.length" class="helper-text">没有找到匹配资料。可以换关键词、放宽范围，或让主助手继续联网核对。</p>
        <article v-for="item in result?.items ?? []" :key="item.kind + ':' + item.id" class="knowledge-item">
          <div class="knowledge-item-head">
            <div>
              <span class="knowledge-type">{{ kindLabel(item.kind) }}</span>
              <span class="knowledge-type">{{ scopeLabel(item.scope) }}</span>
              <span v-for="matched in item.matched_by ?? []" :key="matched" class="knowledge-matched">{{ matched === 'vector' ? '向量命中' : '关键词命中' }}</span>
            </div>
            <span class="knowledge-score">v{{ item.version }}</span>
          </div>
          <h3>{{ item.title }}</h3>
          <p>{{ item.snippet }}</p>
          <div class="knowledge-item-actions">
            <mdui-button variant="text" :disabled="detailBusy" @click="openEntry(item)">查看原文</mdui-button>
            <a v-if="externalUrl(item)" class="app-link" :href="externalUrl(item)!" target="_blank" rel="noopener noreferrer">打开原网页</a>
            <span class="helper-text">{{ item.channels.map(channelLabel).join(' · ') }}</span>
          </div>
        </article>
      </section>

      <aside class="knowledge-aside">
        <section v-if="detail" class="knowledge-detail" aria-labelledby="knowledge-detail-heading">
          <div class="knowledge-detail-head">
            <div>
              <span class="knowledge-type">{{ kindLabel(detail.kind) }}</span>
              <span class="knowledge-type">{{ scopeLabel(detail.scope) }}</span>
            </div>
            <mdui-button-icon aria-label="关闭原文" @click="detail = null"><AppIcon name="close"></AppIcon></mdui-button-icon>
          </div>
          <h2 id="knowledge-detail-heading">{{ detail.title }}</h2>
          <p class="helper-text">版本 v{{ detail.version }}；共 {{ detail.total_characters }} 字符；当前从第 {{ detail.offset }} 字符开始。</p>
          <p v-if="detail.metadata && (detail.metadata.publisher || detail.metadata.published_date || detail.metadata.audience || detail.metadata.effective_status)" class="helper-text">
            <span v-if="detail.metadata.publisher">发布单位：{{ detail.metadata.publisher }}</span>
            <span v-if="detail.metadata.published_date"> · 发布日期：{{ detail.metadata.published_date }}</span>
            <span> · 状态：{{ effectiveLabel(detail.metadata.effective_status) }}</span>
            <span v-if="detail.metadata.audience"> · 适用人群：{{ detail.metadata.audience }}</span>
          </p>
          <pre class="knowledge-content">{{ detail.content }}</pre>
          <div class="knowledge-item-actions">
            <mdui-button v-if="detail.next_offset" variant="outlined" :disabled="detailBusy" @click="readMore">继续读取</mdui-button>
            <a v-if="externalUrl(detail)" class="app-link" :href="externalUrl(detail)!" target="_blank" rel="noopener noreferrer">打开原网页</a>
            <mdui-button v-if="detail.kind === 'document' && detail.status === 'active'" variant="text" @click="archiveCurrent">归档资料</mdui-button>
          </div>
        </section>

        <section class="knowledge-panel" aria-labelledby="knowledge-note-heading">
          <div class="knowledge-panel-head">
            <h2 id="knowledge-note-heading">保存资料</h2>
            <mdui-button variant="text" @click="showNoteForm = !showNoteForm">{{ showNoteForm ? '收起' : '新增' }}</mdui-button>
          </div>
          <form v-if="showNoteForm" class="knowledge-note-form" @submit.prevent="saveNote">
            <mdui-text-field label="标题" :value="note.title" required maxlength="500" @input="note.title = readMduiValue($event)"></mdui-text-field>
            <mdui-text-field label="正文" :value="note.content" required rows="6" autosize max-rows="12" maxlength="500000" @input="note.content = readMduiValue($event)"></mdui-text-field>
            <mdui-select variant="outlined" label="范围" :value="note.scope" @change="note.scope = readMduiValue($event) as 'private' | 'public'">
              <mdui-menu-item value="private">私人库</mdui-menu-item>
              <mdui-menu-item value="public">公共库（管理员）</mdui-menu-item>
            </mdui-select>
            <mdui-text-field label="发布单位（可选）" :value="note.publisher" maxlength="300" placeholder="例如：教务处" @input="note.publisher = readMduiValue($event)"></mdui-text-field>
            <mdui-text-field label="发布日期（可选）" type="date" :value="note.published_date" @input="note.published_date = readMduiValue($event)"></mdui-text-field>
            <mdui-select variant="outlined" label="有效性" :value="note.effective_status" @change="note.effective_status = readMduiValue($event) as 'unknown' | 'effective' | 'expired'">
              <mdui-menu-item value="unknown">未确认</mdui-menu-item>
              <mdui-menu-item value="effective">现行有效</mdui-menu-item>
              <mdui-menu-item value="expired">已失效</mdui-menu-item>
            </mdui-select>
            <mdui-text-field label="适用人群（可选）" :value="note.audience" maxlength="300" placeholder="例如：2024级本科生" @input="note.audience = readMduiValue($event)"></mdui-text-field>
            <mdui-button type="submit" variant="filled" :disabled="saving">{{ saving ? '保存中……' : '保存' }}</mdui-button>
          </form>
          <p v-else class="helper-text">保存私人笔记或由管理员发布公共规定；私人资料不会因保存自动公开。</p>
        </section>

        <section class="knowledge-panel" aria-labelledby="knowledge-web-heading">
          <h2 id="knowledge-web-heading">读取学校网页</h2>
          <p class="helper-text">只接受 gdufs.edu.cn 的公开 HTTPS 页面；保存抓取时间、出处和私人快照，不绕过登录或访问限制。</p>
          <form class="knowledge-web-form" @submit.prevent="readWebPage">
            <mdui-text-field label="学校网页地址" type="url" :value="webUrl" maxlength="2000" placeholder="https://www.gdufs.edu.cn/..." @input="webUrl = readMduiValue($event)"></mdui-text-field>
            <mdui-button type="submit" variant="outlined" :disabled="webBusy">{{ webBusy ? '读取中……' : '读取' }}</mdui-button>
          </form>
        </section>

        <section class="knowledge-panel" aria-labelledby="knowledge-mail-heading">
          <h2 id="knowledge-mail-heading">从邮箱转入资料</h2>
          <template v-if="mail">
            <p class="helper-text">把微信记录或材料从本人已验证邮箱转发到运营邮箱，并在主题加入专属收件码。未配置真实收信时可直接导入 .eml。</p>
            <dl class="knowledge-mail-info">
              <dt>运营收件地址</dt><dd>{{ mail.recipient || '尚未配置' }}</dd>
              <dt>专属收件码</dt><dd>{{ mail.subject_tag || '尚未生成' }}</dd>
              <dt>真实收信</dt><dd>{{ mail.configured ? '已配置' : '未配置，可使用 .eml 手动导入' }}</dd>
            </dl>
            <div class="knowledge-item-actions">
              <mdui-button variant="outlined" :disabled="mailBusy" @click="rotateToken">生成或轮换收件码</mdui-button>
              <mdui-button variant="text" :disabled="mailBusy" @click="emlInput?.click()">导入 .eml</mdui-button>
              <input ref="emlInput" type="file" accept=".eml,message/rfc822" hidden @change="uploadEml" />
            </div>
            <ul v-if="mail.recent.length" class="knowledge-mail-recent">
              <li v-for="receipt in mail.recent" :key="String(receipt.updated_at_epoch) + (receipt.source_id ?? '')">
                <span>{{ receipt.status === 'completed' ? '已入库' : '处理中或需重试' }}</span>
                <small>{{ new Date(receipt.updated_at_epoch * 1000).toLocaleString('zh-CN') }}</small>
                <span v-if="receipt.warnings.length">{{ receipt.warnings.length }} 个附件未解析</span>
              </li>
            </ul>
            <p v-else class="helper-text">还没有转发记录。</p>
          </template>
        </section>

        <p v-if="status" class="knowledge-footnote">{{ status.note }}</p>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.knowledge-page { max-width: 1180px; }
.knowledge-hero { display:flex; justify-content:space-between; gap:24px; align-items:flex-start; flex-wrap:wrap; margin-bottom:18px; }
.knowledge-hero h1 { margin:6px 0 8px; }
.knowledge-hero p { max-width:640px; color:#5c6472; line-height:1.7; }
.knowledge-status { display:flex; gap:8px; flex-wrap:wrap; max-width:440px; }
.knowledge-badge { display:inline-flex; align-items:center; gap:6px; padding:6px 10px; border:1px solid #dbe0ea; border-radius:999px; background:#fff; font-size:12px; color:#4c5668; }
.knowledge-badge[data-state='ready'] { border-color:#b9dec8; color:#216e43; }
.knowledge-badge[data-state='not_configured'], .knowledge-badge[data-state='disabled'] { color:#8a5a16; border-color:#ead9bc; }
.knowledge-search { display:grid; grid-template-columns:minmax(0,2fr) repeat(3,minmax(0,1fr)) auto; gap:10px; align-items:end; padding:16px; border:1px solid #e0e4ed; border-radius:16px; background:#fff; }
.knowledge-search label { display:grid; gap:6px; font-size:12px; color:#586274; }
.knowledge-search input, .knowledge-search select, .knowledge-note-form input, .knowledge-note-form textarea, .knowledge-note-form select, .knowledge-web-form input { min-height:40px; padding:8px 10px; border:1px solid #cbd2df; border-radius:10px; background:#fff; font:inherit; color:inherit; }
.knowledge-evidence-note { margin:14px 0 0; padding:10px 12px; border-left:3px solid #7a86a8; background:#f5f7fc; color:#4f5969; font-size:13px; line-height:1.6; }
.knowledge-channels { display:flex; gap:14px; flex-wrap:wrap; margin:10px 0 18px; font-size:12px; color:#687184; }
.knowledge-channels span[data-state='failed'] { color:#a33b3b; }
.knowledge-channels span[data-state='not_configured'] { color:#8a5a16; }
.knowledge-channels small { display:block; color:#8b93a3; }
.knowledge-layout { display:grid; grid-template-columns:minmax(0,1.55fr) minmax(320px,1fr); gap:22px; align-items:start; }
.knowledge-results h2, .knowledge-panel h2, .knowledge-detail h2 { margin:0 0 10px; }
.knowledge-item { padding:16px 0; border-bottom:1px solid #e7eaf1; }
.knowledge-item-head, .knowledge-detail-head, .knowledge-item-actions, .knowledge-panel-head { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; }
.knowledge-item h3 { margin:8px 0 6px; font-size:16px; }
.knowledge-item p { margin:0 0 10px; color:#4e5868; line-height:1.65; }
.knowledge-type, .knowledge-matched { display:inline-block; margin-right:6px; padding:2px 8px; border-radius:999px; background:#eef1f7; color:#5c6679; font-size:11px; }
.knowledge-matched { background:#e9f3ee; color:#276647; }
.knowledge-score { color:#8a92a1; font-size:12px; }
.knowledge-item-actions { justify-content:flex-start; }
.knowledge-item-actions a, .knowledge-detail a { color:#3559a9; font-size:13px; }
.knowledge-aside { display:grid; gap:16px; }
.knowledge-panel, .knowledge-detail { padding:16px; border:1px solid #e0e4ed; border-radius:16px; background:#fff; }
.knowledge-content { max-height:360px; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; padding:12px; border:1px solid #e8ebf2; border-radius:10px; background:#fafbff; font-family:inherit; font-size:13px; line-height:1.7; }
.knowledge-note-form { display:grid; gap:10px; }
.knowledge-note-form label { display:grid; gap:6px; font-size:12px; color:#586274; }
.knowledge-note-form textarea { resize:vertical; }
.knowledge-web-form { display:flex; gap:8px; }
.knowledge-web-form input { flex:1; }
.knowledge-mail-info { display:grid; grid-template-columns:auto 1fr; gap:6px 12px; margin:10px 0; font-size:13px; }
.knowledge-mail-info dt { color:#6b7384; }
.knowledge-mail-info dd { margin:0; overflow-wrap:anywhere; }
.knowledge-mail-recent { list-style:none; padding:0; display:grid; gap:8px; }
.knowledge-mail-recent li { display:flex; gap:10px; flex-wrap:wrap; font-size:12px; color:#5f6878; }
.knowledge-footnote { color:#8a92a1; font-size:12px; }
@media (max-width: 960px) {
  .knowledge-search { grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
  .knowledge-search label:first-child { grid-column:1 / -1; }
  .knowledge-layout { grid-template-columns:minmax(0,1fr); }
}
@media (max-width: 600px) {
  .knowledge-search { grid-template-columns:minmax(0,1fr); }
}
</style>
