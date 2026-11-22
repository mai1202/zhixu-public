<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ApiError, apiRequest } from '../api/client';
import AppIcon from '../components/AppIcon.vue';
import TimeZoneSelect from '../components/TimeZoneSelect.vue';
import { readMduiChecked, readMduiValue } from '../utils/mdui';
import { confirmTimetableImport, previewTimetableImport, type CourseDraft, type CourseOverride, type PeriodGroup, type TimetableImport } from '../api/timetable';
const router = useRouter();
const route = useRoute();
const timezone = ref('Asia/Shanghai');
const busy = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
interface CourseEdit {
  start_time: string;
  end_time: string;
  title: string;
  teachers: string;
  room: string;
  week_expression: string;
}

interface EditablePeriodGroup {
  periods: string;
  start_time: string;
  end_time: string;
  source_label: string;
}

const importFile = ref<File | null>(null);
const preview = ref<TimetableImport | null>(null);
const week1Monday = ref('');
const weekCount = ref(16);
const importTermCode = ref('');
const applyMode = ref<'merge' | 'replace_term'>('merge');
const anchorConfirmed = ref(false);
const courseEdits = reactive<Record<string, CourseEdit>>({});
const editableProfile = ref<EditablePeriodGroup[]>([]);

const uniqueCourseCells = computed(() => {
  // 保留每个原始单元格一次，合并格不重复编辑。
  const seen = new Set<string>();
  return (preview.value?.draft.courses ?? []).filter(course => {
    if (seen.has(course.source_cell)) return false;
    seen.add(course.source_cell); return true;
  });
});
function setError(error: unknown): void {
  errorMessage.value = error instanceof ApiError ? error.message : '导入失败，请重试。';
  successMessage.value = '';
}
function hydrate(item: TimetableImport): void {
  preview.value = item;
    week1Monday.value = item.draft.week1_monday ?? week1Monday.value;
    weekCount.value = item.draft.week_count;
    importTermCode.value = item.term_code;
    timezone.value = item.draft.timezone;
    editableProfile.value = item.draft.period_profile.map((group) => ({
      periods: group.periods.join(','),
      start_time: group.start_time ?? '',
      end_time: group.end_time ?? '',
      source_label: group.source_label,
    }));
    for (const course of uniqueCourseCells.value) {
      courseEdits[course.source_cell] = {
        start_time: course.start_time ?? '',
        end_time: course.end_time ?? '',
        title: course.title,
        teachers: course.teachers.join(', '),
        room: course.room ?? '',
        week_expression: course.week_expression,
      };
    }
}
onMounted(async () => {
  // 预览保存于服务端，通过独立地址恢复，不依赖浏览器临时对象。
  if (!route.params.id) return;
  try { hydrate((await apiRequest<{ import: TimetableImport }>(`/api/v1/timetable/imports/${route.params.id}`)).import); }
  catch (error) { setError(error); }
});
function chooseFile(event: Event): void {
  importFile.value = (event.target as HTMLInputElement).files?.[0] ?? null;
  preview.value = null;
  anchorConfirmed.value = false;
  errorMessage.value = '';
}

async function previewImport(): Promise<void> {
  if (!importFile.value) return;
  busy.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const result = await previewTimetableImport(importFile.value, {
      week1_monday: week1Monday.value || undefined,
      week_count: weekCount.value,
      timezone: timezone.value,
      term_code: importTermCode.value || undefined,
    });
    hydrate(result.import);
    await router.replace(`/schedule/import/${result.import.id}`);
    successMessage.value = result.already_seen ? '该文件此前已导入，显示原预览记录。' : '已生成只读预览；课程尚未写入日程。';
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

function addPeriodGroup(): void {
  editableProfile.value.push({ periods: '', start_time: '', end_time: '', source_label: '手工补充作息时段' });
}

function movePeriodGroup(index: number, direction: number): void {
  const target = index + direction;
  if (target < 0 || target >= editableProfile.value.length) return;
  // 整行交换，不能把小节和起止时刻拆散。
  const [group] = editableProfile.value.splice(index, 1);
  editableProfile.value.splice(target, 0, group);
}

function sortPeriodGroups(): void {
  // 未填时间的行放到末尾，同一时刻保持用户原顺序。
  editableProfile.value.sort((left, right) => (left.start_time || '99:99').localeCompare(right.start_time || '99:99'));
}

function parsedPeriodProfile(): PeriodGroup[] {
  // 误点新增产生的全空行不阻止提交；只填了一部分仍保留给完整校验处理。
  return editableProfile.value.filter(group => group.periods.trim() || group.start_time || group.end_time).map((group) => ({
    periods: group.periods.split(/[,，\s]+/).map(Number).filter((period) => Number.isInteger(period) && period > 0),
    start_time: group.start_time || null,
    end_time: group.end_time || null,
    source_label: group.source_label,
  }));
}

function makeOverrides(): CourseOverride[] {
  return uniqueCourseCells.value.map((course) => {
    const edit = courseEdits[course.source_cell];
    return {
      source_cell: course.source_cell,
      start_time: edit.start_time || null,
      end_time: edit.end_time || null,
      title: edit.title.trim(),
      display_name: edit.title.trim(),
      teachers: edit.teachers.split(/[,，、]+/).map((name) => name.trim()).filter(Boolean),
      room: edit.room.trim() || null,
      week_expression: edit.week_expression.trim(),
    };
  });
}

async function confirmImport(): Promise<void> {
  if (!preview.value) return;
  busy.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const confirmed = await confirmTimetableImport(preview.value, {
      week1_monday: week1Monday.value,
      week_count: weekCount.value,
      timezone: timezone.value,
      anchor_confirmed: anchorConfirmed.value,
      apply_mode: applyMode.value,
      period_profile: parsedPeriodProfile(),
      course_overrides: makeOverrides(),
    });
    preview.value = confirmed;
    anchorConfirmed.value = false;
    successMessage.value = `已确认 ${confirmed.term_code} 课表并生成课程实例。`;
    await router.replace({ path: '/schedule', query: { imported: confirmed.term_code } });
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}


</script>
<template>
  <section class="page-section timetable-import-page">
    <RouterLink to="/schedule" class="import-back-link app-link">← 返回日程</RouterLink>
    <header class="import-header">
      <div>
        <p class="eyebrow">{{ preview ? '核对课表' : '导入课表' }}</p>
        <h1>{{ preview ? '核对并确认课表' : '把 Excel 课表带进知序' }}</h1>
        <p>先预览课程、周次和作息，确认前不会写入任何日程。</p>
      </div>
      <span v-if="preview" class="import-state">{{ preview.status === 'proposed' ? '等待确认' : '已导入' }}</span>
    </header>

    <p v-if="errorMessage" role="alert" class="error-text inline-message">{{ errorMessage }}</p>
    <p v-if="successMessage" role="status" class="success-text inline-message">{{ successMessage }}</p>

    <form v-if="!preview" class="import-upload" @submit.prevent="previewImport">
      <label class="import-drop-zone">
        <span class="import-drop-icon"><AppIcon name="upload_file"></AppIcon></span>
        <strong>{{ importFile?.name ?? '选择 .xls / .xlsx 课表文件' }}</strong>
        <small>文件只在服务端解析，识别结果会先生成可核对预览。</small>
        <input type="file" accept=".xls,.xlsx" required @change="chooseFile" />
      </label>
      <div class="import-upload-options">
        <mdui-text-field label="学期代码（可自动识别）" :value="importTermCode" @input="importTermCode = readMduiValue($event)"></mdui-text-field>
        <mdui-text-field label="学期周数" type="number" :value="String(weekCount)" min="1" max="30" required @input="weekCount = Number(readMduiValue($event)) || 1"></mdui-text-field>
        <TimeZoneSelect v-model="timezone" />
      </div>
      <mdui-button type="submit" :disabled="busy || !importFile" variant="filled" :loading="busy">
        识别并进入核对
      </mdui-button>
    </form>

    <template v-else>
      <div class="import-overview">
        <div>
          <strong>{{ preview.term_code }}</strong>
          <span>{{ uniqueCourseCells.length }} 个课程格 · 新增 {{ preview.diff.summary.added ?? 0 }} · 更新 {{ preview.diff.summary.changed ?? 0 }} · 移除 {{ preview.diff.summary.removed ?? 0 }}</span>
        </div>
        <mdui-button variant="text" @click="preview = null; router.push('/schedule/import')">重新选择文件</mdui-button>
      </div>

      <ol class="import-steps" aria-label="课表导入步骤">
        <li><span>1</span>学期日期</li>
        <li><span>2</span>课程与周次</li>
        <li><span>3</span>节次作息</li>
        <li><span>4</span>确认范围</li>
      </ol>

      <ul v-if="preview.draft.warnings.length || preview.draft.unrecognized.length" class="warning-list import-warnings">
        <li v-for="warning in preview.draft.warnings" :key="warning">{{ warning }}</li>
        <li v-for="item in preview.draft.unrecognized" :key="item.cell">{{ item.cell }}：{{ item.reason }}</li>
      </ul>

      <form @submit.prevent="confirmImport">
        <section class="import-step-card">
          <div class="import-step-heading"><span>1</span><div><h2>学期日期</h2><p>首周日期和时区决定课程最终落在日历上的哪一天。</p></div></div>
          <div class="schedule-form">
            <mdui-text-field label="第 1 周周一" type="date" :value="week1Monday" required helper="文件没有提供时不会猜测，请按学校教学日历填写。" @input="week1Monday = readMduiValue($event)"></mdui-text-field>
            <mdui-text-field label="学期周数" type="number" :value="String(weekCount)" min="1" max="30" required @input="weekCount = Number(readMduiValue($event)) || 1"></mdui-text-field>
            <TimeZoneSelect v-model="timezone" />
          </div>
        </section>

        <section class="import-step-card">
          <div class="import-step-heading"><span>2</span><div><h2>课程与周次</h2><p>直接在表格里修正课程名、教师、地点和周次；原文可展开核对。</p></div></div>
          <div class="import-table-scroll">
            <table class="import-table">
              <thead><tr><th>星期 / 节次</th><th>课程</th><th>教师</th><th>地点</th><th>周次</th><th>真实时间</th><th>来源</th></tr></thead>
              <tbody>
                <tr v-for="course in uniqueCourseCells" :key="course.source_cell">
                  <td data-label="星期 / 节次">{{ ['周一','周二','周三','周四','周五','周六','周日'][course.weekday - 1] }}<br />第 {{ course.periods.join('、') }} 节</td>
                  <td data-label="课程"><input v-model="courseEdits[course.source_cell].title" aria-label="课程名称" required maxlength="500" /></td>
                  <td data-label="教师"><input v-model="courseEdits[course.source_cell].teachers" aria-label="教师" /></td>
                  <td data-label="地点"><input v-model="courseEdits[course.source_cell].room" aria-label="地点" /></td>
                  <td data-label="周次"><input v-model="courseEdits[course.source_cell].week_expression" aria-label="周次表达" required /></td>
                  <td data-label="真实时间"><div class="course-actual-times"><label>开始<input v-model="courseEdits[course.source_cell].start_time" type="time" aria-label="课程开始时间" :required="Boolean(courseEdits[course.source_cell].end_time)" /></label><label>结束<input v-model="courseEdits[course.source_cell].end_time" type="time" aria-label="课程结束时间" :required="Boolean(courseEdits[course.source_cell].start_time)" /></label></div><small>留空按节次作息；原文明示时间优先。</small></td>
                  <td data-label="来源"><details><summary>原文</summary><p>{{ course.raw_text }}</p></details></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="import-step-card">
          <div class="import-step-heading"><span>3</span><div><h2>节次作息</h2><p>核对每节课的真实开始与结束时间，最终日历按这里生成。</p></div></div>
          <div class="import-table-scroll">
            <table class="import-table period-table">
              <thead><tr><th>小节</th><th>开始</th><th>结束</th><th>来源说明</th><th>操作</th></tr></thead>
              <tbody>
                <tr v-for="(group,index) in editableProfile" :key="index">
                  <td data-label="小节"><input v-model="group.periods" aria-label="小节" :required="Boolean(group.start_time || group.end_time)" /></td>
                  <td data-label="开始"><input v-model="group.start_time" type="time" aria-label="作息开始" :required="Boolean(group.periods.trim() || group.end_time)" /></td>
                  <td data-label="结束"><input v-model="group.end_time" type="time" aria-label="作息结束" :required="Boolean(group.periods.trim() || group.start_time)" /></td>
                  <td data-label="来源说明">{{ group.source_label }}</td>
                  <td data-label="操作"><div class="period-row-actions">
                    <!-- MDUI内部原生按钮的名称来自插槽文本，补充屏幕阅读器可见的行号。 -->
                    <mdui-button variant="text" type="button" :disabled="index === 0" @click="movePeriodGroup(index, -1)">上移<span class="sr-only">作息第{{ index + 1 }}行</span></mdui-button>
                    <mdui-button variant="text" type="button" :disabled="index === editableProfile.length - 1" @click="movePeriodGroup(index, 1)">下移<span class="sr-only">作息第{{ index + 1 }}行</span></mdui-button>
                    <mdui-button variant="text" type="button" @click="editableProfile.splice(index, 1)">删除<span class="sr-only">作息第{{ index + 1 }}行</span></mdui-button>
                  </div></td>
                </tr>
              </tbody>
            </table>
          </div>
          <mdui-button type="button" variant="text" @click="addPeriodGroup">补充作息时段</mdui-button>
          <mdui-button type="button" variant="text" @click="sortPeriodGroups">按开始时间排序</mdui-button>
        </section>

        <section class="import-step-card">
          <div class="import-step-heading"><span>4</span><div><h2>确认更新范围</h2><p>选择合并或替换学期，确认后才会生成课程实例。</p></div></div>
          <mdui-select variant="outlined" label="更新方式" :value="applyMode" @change="applyMode = readMduiValue($event) as 'merge' | 'replace_term'">
            <mdui-menu-item value="merge">合并：更新匹配课程并保留其他课程</mdui-menu-item>
            <mdui-menu-item value="replace_term">替换：归档已移除课程</mdui-menu-item>
          </mdui-select>
          <ul v-if="applyMode === 'replace_term' && preview.diff.removed.length" class="warning-list">
            <li v-for="(item,index) in preview.diff.removed" :key="index">将归档：{{ item.title }} · 周{{ item.weekday }}</li>
          </ul>
        </section>

        <footer class="import-confirm-bar">
          <mdui-checkbox class="checkbox-field" :checked="anchorConfirmed" @change="anchorConfirmed = readMduiChecked($event)">我已核对首周日期、课程、周次及作息。</mdui-checkbox>
          <mdui-button type="submit" variant="filled" :loading="busy" :disabled="busy || preview.status !== 'proposed' || !anchorConfirmed || !week1Monday">确认并生成课程实例</mdui-button>
          <span v-if="!week1Monday">请先填写第 1 周周一。</span>
          <span v-else-if="preview.status === 'confirmed'">这份课表已经导入。</span>
        </footer>
      </form>
    </template>
  </section>
</template>
