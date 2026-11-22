<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue';
import { RouterLink } from 'vue-router';
import { getNotice, type NoticeDetail } from '../api/business';
import { ApiError } from '../api/client';

const props = defineProps<{ notices: Array<{ id: string; title: string; status: string }> }>();
const selectedId = ref('');
const detail = ref<NoticeDetail | null>(null);
const loading = ref(false);
const error = ref('');
let requestVersion = 0;
let disposed = false;

const quotes = computed(() => {
  // 只展示已在原始片段中定位的引用，不把模型摘要当作证据。
  const segments = new Map(detail.value?.segments.map((segment) => [segment.id, segment.text]) ?? []);
  const seen = new Set<string>();
  return (detail.value?.events ?? []).flatMap((event) => Object.values(event.evidence).flat()).filter((item) => {
    const key = `${item.segment_id}:${item.quote}`;
    if (!item.quote || !segments.get(item.segment_id)?.includes(item.quote) || seen.has(key)) return false;
    seen.add(key);
    return true;
  }).slice(0, 12);
});

async function load(id: string): Promise<void> {
  const version = ++requestVersion;
  selectedId.value = id;
  detail.value = null;
  error.value = '';
  loading.value = true;
  try {
    const result = await getNotice(id);
    if (!disposed && version === requestVersion) detail.value = result;
  } catch (failure) {
    if (!disposed && version === requestVersion) error.value = failure instanceof ApiError ? failure.message : '证据详情暂时无法读取。';
  } finally {
    if (!disposed && version === requestVersion) loading.value = false;
  }
}

watch(() => props.notices.map((notice) => notice.id).join(','), () => {
  // 定时刷新返回相同索引时不重复请求，切换原文后清除上一份证据。
  requestVersion += 1;
  selectedId.value = '';
  detail.value = null;
  error.value = '';
  loading.value = false;
});
onUnmounted(() => { disposed = true; requestVersion += 1; });
</script>

<template>
  <section class="notice-evidence" aria-label="证据与影响">
    <h3>证据与影响</h3>
    <p class="helper-text">查看通知已经关联的事务、课程与核验原文。</p>
    <div class="evidence-actions">
      <mdui-button v-for="notice in notices" :key="notice.id" variant="outlined" @click="load(notice.id)">
        {{ notice.title }}{{ notice.status === 'active' ? '' : '（已归档）' }}
      </mdui-button>
    </div>
    <p v-if="loading" role="status">正在读取证据和关联对象……</p>
    <div v-if="error" role="alert" class="error-text">
      {{ error }} <mdui-button variant="text" @click="load(selectedId)">重试</mdui-button>
    </div>
    <template v-if="detail">
      <h4>关联事务</h4>
      <ul v-if="detail.tasks.length">
        <li v-for="task in detail.tasks" :key="task.id"><RouterLink :to="`/tasks/${task.id}`">{{ task.title }}</RouterLink></li>
      </ul>
      <p v-else class="helper-text">这条通知尚未关联事务。</p>
      <h4>关联课程</h4>
      <ul v-if="detail.courses.length">
        <li v-for="(course, index) in detail.courses" :key="`${course.occurrence_id ?? course.course_series_id}:${index}`">
          <RouterLink :to="{ path: '/courses', query: { focus: course.occurrence_id ?? course.course_series_id, date: course.local_date ?? undefined } }">
            {{ course.title }}<template v-if="course.local_date"> · {{ course.local_date }} {{ course.start }}–{{ course.end }}</template>
          </RouterLink>
        </li>
      </ul>
      <p v-else class="helper-text">这条通知尚未关联课程。</p>
      <h4>原文证据</h4>
      <blockquote v-for="quote in quotes" :key="`${quote.segment_id}:${quote.quote}`">{{ quote.quote }}</blockquote>
      <p v-if="!quotes.length" class="helper-text">当前没有可定位的事项引文，请对照通知原文核验。</p>
      <details v-if="detail.operations.length">
        <summary>查看变更轨迹（{{ detail.operations.length }}）</summary>
        <ul><li v-for="operation in detail.operations" :key="operation.id">{{ operation.summary }}</li></ul>
      </details>
    </template>
  </section>
</template>

<style scoped>
/* 复用页面色彩变量，长引文与长标题在手机上自然换行。 */
.notice-evidence { margin: 24px 0; padding: 18px; border: 1px solid var(--border-color, #ddd); border-radius: 12px; overflow-wrap: anywhere; }
.evidence-actions { display: flex; flex-wrap: wrap; gap: 8px; }
li, blockquote { line-height: 1.8; }
blockquote { margin: 12px 0; padding-left: 14px; border-left: 3px solid #b5a0d0; white-space: pre-wrap; }
</style>
