<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { undoChange } from '../api/business';
import { ApiError } from '../api/client';
import { getCourseDetail, type CourseDetail } from '../api/ops';
import {
  applyCourseException,
  archiveCourseSeries,
  archiveScheduleEntry,
  createCourseMakeup,
  createCourseSeries,
  createScheduleEntry,
  listSchedule,
  listScheduleYear,
  parseScheduleWriteConflicts,
  restoreScheduleEntry,
  updateCourseSeries,
  undoCourseException,
  updateScheduleEntry,
  type ScheduleItem,
  type ScheduleResponse,
  type ScheduleWriteConflict,
} from '../api/timetable';
import AppIcon from '../components/AppIcon.vue';
import AgentInsightPanel from '../components/AgentInsightPanel.vue';
import ScheduleCalendar from '../components/ScheduleCalendar.vue';
import SingleChoiceSegmented from '../components/SingleChoiceSegmented.vue';
import TimeZoneSelect from '../components/TimeZoneSelect.vue';
import { courseTimesForPeriods } from '../utils/courseTimes';

type CalendarView = 'day' | 'week' | 'month' | 'year';

const route = useRoute();
const router = useRouter();
const today = toLocalDate(new Date());
const requestedFocusDate = typeof route.query.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(route.query.date)
  ? route.query.date
  : '';
const anchorDate = ref(requestedFocusDate || today);
const viewMode = ref<CalendarView>(window.matchMedia('(max-width: 760px)').matches ? 'day' : 'week');
const timezone = ref(Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai');
const schedule = ref<ScheduleResponse | null>(null);
const loading = ref(false);
const busy = ref(false);
const errorMessage = ref('');
const successMessage = ref('');
const searchQuery = ref('');
const conflicts = ref<ScheduleWriteConflict[]>([]);
const editorPanel = ref<(HTMLElement & { value?: string | string[] }) | null>(null);
const entryTitle = ref('');
const entryKind = ref<'busy_block' | 'all_day' | 'deadline_marker'>('busy_block');
const entryDate = ref(today);
const entryEndDate = ref(today);
const entryStart = ref('09:00');
const entryEnd = ref('10:00');
const pendingConflict = ref(false);
const editingEntry = ref<ScheduleItem | null>(null);
const selectedItem = ref<ScheduleItem | null>(null);
const courseDetail = ref<CourseDetail | null>(null);
const detailLoading = ref(false);
const lastArchivedEntry = ref<ScheduleItem | null>(null);
const lastException = ref<{ id: string; version: number } | null>(null);
const rescheduleItem = ref<ScheduleItem | null>(null);
const replacementDate = ref('');
const replacementStart = ref('');
const replacementEnd = ref('');
const replacementRoom = ref('');
const makeupItem = ref<ScheduleItem | null>(null);
const makeupDate = ref('');
const makeupStart = ref('16:00');
const makeupEnd = ref('17:40');
const makeupRoom = ref('');
const makeupReason = ref('');
const courseEditorOpen = ref(false);
const editingSeriesId = ref('');
const editingSeriesVersion = ref(0);
const formCourseTermId = ref('');
const formCourseTitle = ref('');
const formCourseTeachers = ref('');
const formCourseRoom = ref('');
const formCourseWeekday = ref(1);
const formCourseStartPeriod = ref(1);
const formCourseEndPeriod = ref(2);
const formCourseStartTime = ref('');
const formCourseEndTime = ref('');
const formCourseUseActualTime = ref(false);
const formCourseStartWeek = ref(1);
const formCourseEndWeek = ref(18);
const lastCourseChange = ref<{ id: string; label: string } | null>(null);

const visibleRange = computed(() => rangeFor(anchorDate.value, viewMode.value));
const rangeLabel = computed(() => {
  if (viewMode.value === 'year') return `${anchorDate.value.slice(0, 4)} 年`;
  if (viewMode.value === 'day') return `${formatLongDate(anchorDate.value)} · ${weekdayLabel(anchorDate.value)}`;
  if (viewMode.value === 'month') {
    const date = parseDate(anchorDate.value);
    return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`;
  }
  const start = parseDate(visibleRange.value.start);
  const end = parseDate(visibleRange.value.end);
  return `${start.getMonth() + 1} 月 ${start.getDate()} 日 – ${end.getMonth() + 1} 月 ${end.getDate()} 日`;
});
const compactRangeLabel = computed(() => {
  if (viewMode.value === 'year') return `${anchorDate.value.slice(0, 4)} 年`;
  // 手机工具栏空间有限，只保留当日或本区间的核心日期，避免与视图切换重叠。
  if (viewMode.value === 'day') {
    return `${Number(anchorDate.value.slice(5, 7))}/${Number(anchorDate.value.slice(8, 10))} · ${weekdayLabel(anchorDate.value)}`;
  }
  if (viewMode.value === 'month') {
    const date = parseDate(anchorDate.value);
    return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月`;
  }
  return `${Number(visibleRange.value.start.slice(5, 7))}/${Number(visibleRange.value.start.slice(8, 10))}–${Number(visibleRange.value.end.slice(5, 7))}/${Number(visibleRange.value.end.slice(8, 10))}`;
});
const selectedItemTime = computed(() => selectedItem.value ? itemTime(selectedItem.value) : '');
const calendarItems = computed(() => {
  // 两个入口共用完整时间线，不因旧“课程”路径隐藏个人日程。
  const sourceItems = schedule.value?.items ?? [];
  const query = searchQuery.value.trim().toLocaleLowerCase();
  if (!query) return sourceItems;
  return sourceItems.filter((item) => (
    [item.title, item.room, item.teachers?.join(' '), item.kind].filter(Boolean).join(' ').toLocaleLowerCase().includes(query)
  ));
});
const calendarDeadlines = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase();
  if (!query) return schedule.value?.deadlines ?? [];
  return (schedule.value?.deadlines ?? []).filter((deadline) => deadline.title.toLocaleLowerCase().includes(query));
});
const currentTerm = computed(() => {
  const terms = schedule.value?.terms ?? [];
  return terms.find((term) => {
    const start = parseDate(term.week1_monday);
    const end = new Date(start);
    end.setDate(end.getDate() + term.week_count * 7 - 1);
    return term.week1_monday <= anchorDate.value && anchorDate.value <= toLocalDate(end);
  }) ?? terms[0] ?? null;
});

onMounted(() => {
  void refreshSchedule();
});

// 从首页/事务页带日期和焦点跳转时，组件可能复用；必须同步查询参数而不是只读首次值。
watch(() => route.fullPath, () => {
  const requestedDate = typeof route.query.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(route.query.date)
    ? route.query.date
    : '';
  if (requestedDate) anchorDate.value = requestedDate;
  selectedItem.value = null;
  void refreshSchedule();
});

function parseDate(value: string): Date {
  return new Date(`${value}T12:00:00`);
}

function toLocalDate(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function addDays(value: string, days: number): string {
  const date = parseDate(value);
  date.setDate(date.getDate() + days);
  return toLocalDate(date);
}

function startOfWeek(value: string): string {
  const date = parseDate(value);
  date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
  return toLocalDate(date);
}

function rangeFor(value: string, view: CalendarView): { start: string; end: string } {
  if (view === 'year') return { start: `${value.slice(0, 4)}-01-01`, end: `${value.slice(0, 4)}-12-31` };
  if (view === 'day') return { start: value, end: value };
  if (view === 'week') {
    const start = startOfWeek(value);
    return { start, end: addDays(start, 6) };
  }
  const date = parseDate(value);
  const monthStart = toLocalDate(new Date(date.getFullYear(), date.getMonth(), 1, 12));
  const gridStart = startOfWeek(monthStart);
  return { start: gridStart, end: addDays(gridStart, 41) };
}

function formatLongDate(value: string): string {
  const date = parseDate(value);
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

function weekdayLabel(value: string): string {
  return ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][parseDate(value).getDay()];
}

async function refreshSchedule(): Promise<void> {
  loading.value = true;
  errorMessage.value = '';
  const selectedId = selectedItem.value?.id;
  try {
    schedule.value = viewMode.value === 'year' ? await listScheduleYear(parseDate(anchorDate.value).getFullYear())
      : await listSchedule(visibleRange.value.start, visibleRange.value.end);
    selectedItem.value = selectedId
      ? schedule.value.items.find((item) => item.id === selectedId) ?? null
      : null;
    const focusId = typeof route.query.focus === 'string' ? route.query.focus : '';
    if (!selectedId && focusId) {
      const focused = schedule.value.items.find((item) => item.id === focusId);
      if (focused) await selectScheduleItem(focused);
    }
  } catch (error) {
    setError(error);
  } finally {
    loading.value = false;
  }
}

function setError(error: unknown): void {
  errorMessage.value = error instanceof ApiError ? error.message : '操作失败，请稍后重试。';
  successMessage.value = '';
  conflicts.value = error instanceof ApiError ? parseScheduleWriteConflicts(error.details?.conflicts) : [];
}

function elementValue(event: Event): string {
  // MDUI 自定义元素把最新值放在宿主元素上，统一读取可避免模板里出现临时类型断言。
  return String((event.currentTarget as HTMLElement & { value?: string }).value ?? '');
}

function changeEntryKind(event: Event): void {
  const value = elementValue(event);
  if (value === 'busy_block' || value === 'all_day' || value === 'deadline_marker') {
    entryKind.value = value;
  }
}

function setViewMode(value: string): void {
  if (value !== 'day' && value !== 'week' && value !== 'month' && value !== 'year') return;
  if (value === viewMode.value) return;
  viewMode.value = value;
  selectedItem.value = null;
  void refreshSchedule();
}

function movePeriod(direction: number): void {
  if (viewMode.value === 'day') {
    anchorDate.value = addDays(anchorDate.value, direction);
  } else if (viewMode.value === 'week') {
    anchorDate.value = addDays(anchorDate.value, direction * 7);
  } else if (viewMode.value === 'year') {
    anchorDate.value = `${parseDate(anchorDate.value).getFullYear() + direction}-01-01`;
  } else {
    const date = parseDate(anchorDate.value);
    anchorDate.value = toLocalDate(new Date(date.getFullYear(), date.getMonth() + direction, 1, 12));
  }
  selectedItem.value = null;
  void refreshSchedule();
}

function goToday(): void {
  anchorDate.value = today;
  selectedItem.value = null;
  void refreshSchedule();
}

function selectDay(date: string): void {
  anchorDate.value = date;
  viewMode.value = 'day';
  selectedItem.value = null;
  void refreshSchedule();
}

async function selectScheduleItem(item: ScheduleItem): Promise<void> {
  selectedItem.value = item;
  courseDetail.value = null;
  if (item.item_type === 'course') {
    detailLoading.value = true;
    try {
      // 课程详情单独按需加载，日历首屏不因三端关联查询变慢。
      courseDetail.value = await getCourseDetail(item.id);
    } catch {
      courseDetail.value = null;
    } finally {
      detailLoading.value = false;
    }
  }
}

function openEditorPanel(): void {
  if (!editorPanel.value) return;
  // 非手风琴模式下 MDUI 使用字符串数组表示展开项，这里显式展开个人安排表单。
  editorPanel.value.value = ['entry-editor'];
  requestAnimationFrame(() => editorPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
}

function openCreateEditor(): void {
  editingEntry.value = null;
  entryTitle.value = '';
  entryKind.value = 'busy_block';
  entryDate.value = anchorDate.value;
  entryEndDate.value = anchorDate.value;
  entryStart.value = '09:00';
  entryEnd.value = '10:00';
  pendingConflict.value = false;
  openEditorPanel();
}

function weekdayNumber(value: string): number {
  // 业务接口把周一作为 1、周日作为 7，与 JavaScript 的 0–6 明确转换。
  const day = parseDate(value).getDay();
  return day === 0 ? 7 : day;
}

function dateForTermWeek(term: ScheduleResponse['terms'][number], week: number, weekday: number): string {
  const start = parseDate(term.week1_monday);
  start.setDate(start.getDate() + (week - 1) * 7 + weekday - 1);
  return toLocalDate(start);
}

function openNewCourseEditor(): void {
  if (!currentTerm.value) {
    errorMessage.value = '当前没有可用的学期，请先导入课表或确认学期起始周。';
    return;
  }
  const term = currentTerm.value;
  const start = parseDate(term.week1_monday);
  const viewed = parseDate(anchorDate.value);
  const currentWeek = Math.max(1, Math.min(term.week_count, Math.floor((viewed.getTime() - start.getTime()) / 604_800_000) + 1));
  editingSeriesId.value = '';
  editingSeriesVersion.value = 0;
  formCourseTermId.value = term.id;
  formCourseTitle.value = '';
  formCourseTeachers.value = '';
  formCourseRoom.value = '';
  formCourseWeekday.value = weekdayNumber(anchorDate.value);
  formCourseStartPeriod.value = 1;
  formCourseEndPeriod.value = 2;
  syncCourseTimes();
  formCourseStartWeek.value = Math.min(currentWeek, term.week_count);
  formCourseEndWeek.value = term.week_count;
  courseEditorOpen.value = true;
}

async function openEditCourseSeries(item: ScheduleItem): Promise<void> {
  detailLoading.value = true;
  try {
    const detail = courseDetail.value?.occurrence.occurrence_id === item.id
      ? courseDetail.value
      : await getCourseDetail(item.id);
    const series = detail.series;
    if (series.status !== 'active') {
      errorMessage.value = '这门课程已经归档；请从工作台撤销归档或让 Agent 恢复后再修改。';
      return;
    }
    const periods = [...series.periods].sort((left, right) => left - right);
    editingSeriesId.value = series.id;
    editingSeriesVersion.value = series.version;
    formCourseTermId.value = series.term_id;
    formCourseTitle.value = series.title;
    formCourseTeachers.value = series.teachers.join('、');
    formCourseRoom.value = series.room ?? '';
    formCourseWeekday.value = series.weekday;
    formCourseStartPeriod.value = periods[0] ?? 1;
    formCourseEndPeriod.value = periods.at(-1) ?? 2;
    syncCourseTimes();
    if (series.start_time && series.end_time) {
      formCourseStartTime.value = series.start_time;
      formCourseEndTime.value = series.end_time;
      formCourseUseActualTime.value = true;
    }
    formCourseStartWeek.value = Math.min(...series.weeks);
    formCourseEndWeek.value = Math.max(...series.weeks);
    selectedItem.value = null;
    courseEditorOpen.value = true;
  } catch (error) {
    setError(error);
  } finally {
    detailLoading.value = false;
  }
}

function periodRange(start: number, end: number): number[] {
  return Array.from({ length: end - start + 1 }, (_, index) => start + index);
}

function syncCourseTimes(): void {
  const term = schedule.value?.terms.find(item => item.id === formCourseTermId.value);
  const start = Number(formCourseStartPeriod.value), end = Number(formCourseEndPeriod.value);
  const periods = end >= start && start >= 1 && end <= 30 ? periodRange(start, end) : [];
  const resolved = courseTimesForPeriods(periods, term?.period_profile ?? []);
  // 切换小节后立即更新，部分作息组无法推导时清空，提示用户填写真实时刻。
  formCourseStartTime.value = resolved?.start ?? '';
  formCourseEndTime.value = resolved?.end ?? '';
  formCourseUseActualTime.value = !resolved;
}

async function submitCourseSeries(): Promise<void> {
  const term = schedule.value?.terms.find((item) => item.id === formCourseTermId.value) ?? currentTerm.value;
  if (!term) {
    errorMessage.value = '找不到课程所属学期，请刷新后重试。';
    return;
  }
  const startPeriod = Math.trunc(formCourseStartPeriod.value);
  const endPeriod = Math.trunc(formCourseEndPeriod.value);
  const startWeek = Math.trunc(formCourseStartWeek.value);
  const endWeek = Math.trunc(formCourseEndWeek.value);
  if (startPeriod < 1 || endPeriod > 30 || endPeriod < startPeriod) {
    errorMessage.value = '节次范围无效，结束节次不能早于开始节次。';
    return;
  }
  if (startWeek < 1 || endWeek > term.week_count || endWeek < startWeek) {
    errorMessage.value = `教学周范围必须在 1–${term.week_count} 周内。`;
    return;
  }
  if (!formCourseStartTime.value || !formCourseEndTime.value || formCourseEndTime.value <= formCourseStartTime.value) {
    errorMessage.value = '请填写有效的课程开始、结束时间；结束必须晚于开始。';
    return;
  }
  busy.value = true;
  errorMessage.value = '';
  successMessage.value = '';
  try {
    const title = formCourseTitle.value.trim();
    const payload = {
      title,
      display_name: title,
      teachers: formCourseTeachers.value.split(/[,，、;；]+/).map((item) => item.trim()).filter(Boolean),
      room: formCourseRoom.value.trim() || null,
      weekday: formCourseWeekday.value,
      periods: periodRange(startPeriod, endPeriod),
      start_time: formCourseUseActualTime.value ? formCourseStartTime.value : null,
      end_time: formCourseUseActualTime.value ? formCourseEndTime.value : null,
      week_numbers: periodRange(startWeek, endWeek),
      raw_text: editingSeriesId.value ? '用户在课程页修改常规课程。' : '用户在课程页手动新增常规课程。',
    };
    const result = editingSeriesId.value
      ? await updateCourseSeries(editingSeriesId.value, editingSeriesVersion.value, payload)
      : await createCourseSeries({ ...payload, term_id: term.id });
    lastCourseChange.value = {
      id: result.change_set.id,
      label: editingSeriesId.value ? '课程系列修改' : '新增课程',
    };
    conflicts.value = result.conflicts ?? [];
    courseEditorOpen.value = false;
    selectedItem.value = null;
    courseDetail.value = null;
    anchorDate.value = dateForTermWeek(term, startWeek, formCourseWeekday.value);
    const baseMessage = editingSeriesId.value
      ? '课程系列已更新；未受单次例外保护的实例已按新规则重建。'
      : `课程已新增，共生成 ${Math.max(0, endWeek - startWeek + 1)} 次上课安排。`;
    successMessage.value = conflicts.value.length
      ? `${baseMessage} 同时检测到 ${conflicts.value.length} 个时间冲突，请在课表下方核对。`
      : baseMessage;
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

async function archiveSelectedCourse(): Promise<void> {
  const item = selectedItem.value;
  if (!item || item.item_type !== 'course') return;
  if (!window.confirm(`确认归档整门课程“${item.title}”？未来常规课程会从课表中隐藏，之后仍可撤销。`)) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    const detail = courseDetail.value?.occurrence.occurrence_id === item.id
      ? courseDetail.value
      : await getCourseDetail(item.id);
    const result = await archiveCourseSeries(detail.series.id, detail.series.version);
    lastCourseChange.value = { id: result.change_set.id, label: '课程归档' };
    selectedItem.value = null;
    courseDetail.value = null;
    successMessage.value = '整门课程已归档，历史实例与审计记录仍保留。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

async function undoLastCourseChange(): Promise<void> {
  if (!lastCourseChange.value) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    await undoChange(lastCourseChange.value.id);
    successMessage.value = `已撤销${lastCourseChange.value.label}。`;
    lastCourseChange.value = null;
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

function editEntry(item: ScheduleItem): void {
  editingEntry.value = item;
  entryTitle.value = item.title;
  entryKind.value = item.kind ?? 'all_day';
  entryDate.value = item.local_date;
  entryEndDate.value = item.local_date;
  const originalStart = item.original_start_at ?? item.start_at;
  const originalEnd = item.original_end_at ?? item.end_at;
  if (originalStart) {
    entryDate.value = formatLocalDate(originalStart, item.timezone ?? timezone.value);
    entryStart.value = formatClock(originalStart);
  }
  if (originalEnd) {
    entryEndDate.value = formatLocalDate(originalEnd, item.timezone ?? timezone.value);
    entryEnd.value = formatClock(originalEnd);
  }
  pendingConflict.value = false;
  selectedItem.value = null;
  openEditorPanel();
}

function stopEditingEntry(): void {
  editingEntry.value = null;
  entryTitle.value = '';
  pendingConflict.value = false;
  if (editorPanel.value) editorPanel.value.value = [];
}

function offsetDateTime(date: string, clock: string, zoneName: string): string {
  const [year, month, day] = date.split('-').map(Number);
  const [hour, minute] = clock.split(':').map(Number);
  const naive = Date.UTC(year, month - 1, day, hour, minute);
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: zoneName,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  });
  const parts = Object.fromEntries(formatter.formatToParts(new Date(naive)).map((part) => [part.type, part.value]));
  const zonedAsUtc = Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute));
  const offsetMinutes = Math.round((zonedAsUtc - naive) / 60_000);
  const sign = offsetMinutes < 0 ? '-' : '+';
  const absolute = Math.abs(offsetMinutes);
  return `${date}T${clock}:00${sign}${String(Math.floor(absolute / 60)).padStart(2, '0')}:${String(absolute % 60).padStart(2, '0')}`;
}

async function submitEntry(allowConflict = false): Promise<void> {
  busy.value = true;
  errorMessage.value = '';
  conflicts.value = [];
  try {
    const payload = {
      title: entryTitle.value.trim(),
      kind: entryKind.value,
      local_date: entryDate.value,
      timezone: timezone.value,
      ...(entryKind.value === 'busy_block'
        ? {
            start_at: offsetDateTime(entryDate.value, entryStart.value, timezone.value),
            end_at: offsetDateTime(entryEndDate.value, entryEnd.value, timezone.value),
            allow_conflict: allowConflict,
          }
        : {}),
    };
    if (editingEntry.value) {
      await updateScheduleEntry(editingEntry.value, { ...payload, allow_conflict: allowConflict });
      editingEntry.value = null;
    } else {
      await createScheduleEntry(payload);
    }
    anchorDate.value = entryDate.value;
    pendingConflict.value = false;
    successMessage.value = '日程已保存。';
    stopEditingEntry();
    await refreshSchedule();
  } catch (error) {
    setError(error);
    pendingConflict.value = error instanceof ApiError && error.status === 409 && conflicts.value.length > 0;
  } finally {
    busy.value = false;
  }
}

async function archiveEntry(item: ScheduleItem): Promise<void> {
  busy.value = true;
  errorMessage.value = '';
  try {
    lastArchivedEntry.value = await archiveScheduleEntry(item);
    selectedItem.value = null;
    courseDetail.value = null;
    successMessage.value = '个人日程已归档，可撤销最近一次归档。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

async function restoreLastEntry(): Promise<void> {
  if (!lastArchivedEntry.value) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    await restoreScheduleEntry(lastArchivedEntry.value);
    lastArchivedEntry.value = null;
    successMessage.value = '日程已恢复。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

async function cancelOccurrence(item: ScheduleItem): Promise<void> {
  if (!window.confirm(`确认只取消“${item.title}”在 ${item.local_date} 的这一次课程？`)) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    const result = await applyCourseException(item, 'cancel', '用户在课表中确认取消本次课程。');
    lastException.value = result.exception;
    selectedItem.value = null;
    successMessage.value = '已取消这一次课程，课程系列保持不变。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

function openReschedule(item: ScheduleItem): void {
  rescheduleItem.value = item;
  replacementDate.value = item.local_date;
  replacementStart.value = formatClock(item.start_at) || '09:00';
  replacementEnd.value = formatClock(item.end_at) || '10:00';
  replacementRoom.value = item.room ?? '';
  selectedItem.value = null;
}

async function submitReschedule(): Promise<void> {
  const item = rescheduleItem.value;
  if (!item) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    const result = await applyCourseException(
      item,
      'reschedule',
      '用户在课表中确认改期本次课程。',
      {
        start_at: offsetDateTime(replacementDate.value, replacementStart.value, timezone.value),
        end_at: offsetDateTime(replacementDate.value, replacementEnd.value, timezone.value),
        room: replacementRoom.value.trim() || undefined,
      },
    );
    lastException.value = result.exception;
    rescheduleItem.value = null;
    successMessage.value = '已改期这一次课程，课程系列保持不变。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

function openMakeup(item: ScheduleItem): void {
  makeupItem.value = item;
  makeupDate.value = item.local_date;
  makeupStart.value = '16:00';
  makeupEnd.value = '17:40';
  makeupRoom.value = item.room ?? '';
  makeupReason.value = '根据课程通知安排补课。';
  selectedItem.value = null;
}

async function submitMakeup(): Promise<void> {
  const item = makeupItem.value;
  if (!item) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    await createCourseMakeup(item, {
      local_date: makeupDate.value,
      start_at: offsetDateTime(makeupDate.value, makeupStart.value, timezone.value),
      end_at: offsetDateTime(makeupDate.value, makeupEnd.value, timezone.value),
      room: makeupRoom.value.trim() || undefined,
      reason: makeupReason.value.trim(),
    });
    makeupItem.value = null;
    successMessage.value = '补课已作为独立实例写入日程，常规课表不变。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

async function undoException(): Promise<void> {
  if (!lastException.value) return;
  busy.value = true;
  errorMessage.value = '';
  try {
    await undoCourseException(lastException.value.id, lastException.value.version);
    lastException.value = null;
    successMessage.value = '课程单次例外已撤销。';
    await refreshSchedule();
  } catch (error) {
    setError(error);
  } finally {
    busy.value = false;
  }
}

function formatClock(value: string | null): string {
  if (!value) return '';
  if (/^\d{2}:\d{2}$/.test(value)) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: timezone.value,
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(new Date(value));
}

function formatLocalDate(value: string, zoneName: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: zoneName,
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(value));
  const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${fields.year}-${fields.month}-${fields.day}`;
}

function itemTime(item: ScheduleItem): string {
  if (!item.start_at) return '全天';
  return `${formatClock(item.start_at)}${item.end_at ? `–${formatClock(item.end_at)}` : ''}`;
}

function itemMeta(item: ScheduleItem): string {
  return [
    item.room,
    item.teachers?.length ? item.teachers.join('、') : '',
    item.periods?.length ? `第 ${item.periods.join('、')} 节` : '',
  ].filter(Boolean).join(' · ');
}

function linkDestination(link: {
  from_type: string;
  from_id: string;
  to_type: string;
  to_id: string;
}): string | { path: string; query?: Record<string, string> } {
  // 从课程详情跳转时保留对端对象 ID，避免只落到列表页后再次查找。
  if (link.from_type === 'notice' || link.to_type === 'notice') {
    return {
      path: '/inbox',
      query: {
        panel: 'notices',
        notice: link.from_type === 'notice' ? link.from_id : link.to_id,
      },
    };
  }
  if (link.from_type === 'task' || link.to_type === 'task') {
    return `/tasks/${link.from_type === 'task' ? link.from_id : link.to_id}`;
  }
  if (link.from_type === 'course_occurrence' || link.to_type === 'course_occurrence') {
    return {
      path: '/schedule',
      query: { focus: link.from_type === 'course_occurrence' ? link.from_id : link.to_id },
    };
  }
  return '/courses';
}
</script>

<template>
  <section class="page-section schedule-page" aria-labelledby="schedule-heading">
    <header class="schedule-header">
      <div>
        <h1 id="schedule-heading">日程</h1>
      </div>
      <div class="schedule-header-actions">
        <mdui-button variant="text" @click="router.push('/schedule/import')">导入课表</mdui-button>
        <mdui-dropdown>
          <mdui-button slot="trigger" variant="filled">
            <AppIcon name="add" slot="icon"></AppIcon>
            新建
          </mdui-button>
          <mdui-menu>
            <mdui-menu-item @click="openCreateEditor">添加个人安排</mdui-menu-item>
            <mdui-menu-item @click="openNewCourseEditor">新建常规课程</mdui-menu-item>
          </mdui-menu>
        </mdui-dropdown>
      </div>
    </header>

    <div class="schedule-toolbar">
      <SingleChoiceSegmented
        class="schedule-view-switch"
        :model-value="viewMode"
        aria-label="日历视图"
        @update:model-value="setViewMode"
      >
        <mdui-segmented-button value="day">日</mdui-segmented-button>
        <mdui-segmented-button value="week">周</mdui-segmented-button>
        <mdui-segmented-button value="month">月</mdui-segmented-button>
        <mdui-segmented-button value="year">年</mdui-segmented-button>
      </SingleChoiceSegmented>
      <div class="schedule-navigation">
        <mdui-button-icon aria-label="上一个时间段" :disabled="loading" @click="movePeriod(-1)">
          <AppIcon name="chevron_left"></AppIcon>
        </mdui-button-icon>
        <strong>
          <span class="schedule-range-wide">{{ rangeLabel }}</span>
          <span class="schedule-range-compact">{{ compactRangeLabel }}</span>
        </strong>
        <mdui-button-icon aria-label="下一个时间段" :disabled="loading" @click="movePeriod(1)">
          <AppIcon name="chevron_right"></AppIcon>
        </mdui-button-icon>
        <mdui-button class="schedule-today-button" variant="text" :disabled="loading" @click="goToday">今天</mdui-button>
      </div>
      <!-- 搜索只保留一个原生输入框，避免组件填充层叠加外部背景。 -->
      <input
        class="schedule-search"
        type="search"
        v-model="searchQuery"
        placeholder="搜索课程、任务或教室"
        aria-label="搜索课程、日程、任务、教师或教室"
      />
    </div>

    <p v-if="errorMessage" class="error-text inline-message" role="alert">{{ errorMessage }}</p>
    <p v-if="successMessage" class="success-text inline-message" role="status">{{ successMessage }}</p>

    <AgentInsightPanel
      class="schedule-insight"
      module="course"
      compact
      hide-empty
      empty-text="当前没有调课、停课或课程冲突；Agent 仍会在后台持续检查课表。"
    />

    <ul v-if="conflicts.length" class="warning-list conflict-list">
      <li v-for="(conflict, index) in conflicts" :key="`${conflict.object_id}-${index}`">
        {{ formatLocalDate(conflict.start_at, timezone) }}：与「{{ conflict.title }}」在
        {{ formatClock(conflict.start_at) }}–{{ formatClock(conflict.end_at) }} 重叠
      </li>
    </ul>

    <div class="schedule-calendar-shell schedule-calendar-shell--fill" :aria-busy="loading" :data-view="viewMode">
      <div v-if="loading && !schedule" class="schedule-calendar-loading" role="status">
        <mdui-circular-progress></mdui-circular-progress>
        <span>正在加载日历……</span>
      </div>
      <ScheduleCalendar
        v-else
        :items="calendarItems"
        :deadlines="calendarDeadlines"
        :anchor-date="anchorDate"
        :view="viewMode"
        @select-day="selectDay"
        @select-item="selectScheduleItem"
      />
      <p v-if="searchQuery && !calendarItems.length && !calendarDeadlines.length" class="schedule-search-empty">
        没有匹配的课程、个人日程或任务截止提醒。
      </p>
    </div>

    <mdui-collapse ref="editorPanel" class="schedule-editor">
      <mdui-collapse-item value="entry-editor">
        <mdui-list-item
          slot="header"
          :headline="editingEntry ? '编辑个人安排' : '添加个人安排'"
        >
          <AppIcon slot="icon" name="edit_calendar"></AppIcon>
        </mdui-list-item>
        <form class="schedule-editor-form" @submit.prevent="submitEntry(false)">
          <mdui-text-field
            label="标题"
            required
            maxlength="500"
            :value="entryTitle"
            @input="entryTitle = elementValue($event)"
          ></mdui-text-field>
          <mdui-select variant="outlined" label="类型" :value="entryKind" @change="changeEntryKind">
            <mdui-menu-item value="busy_block">占用时段</mdui-menu-item>
            <mdui-menu-item value="all_day">全天安排</mdui-menu-item>
            <mdui-menu-item value="deadline_marker">仅截止日期</mdui-menu-item>
          </mdui-select>
          <mdui-text-field
            label="日期"
            type="date"
            required
            :value="entryDate"
            @input="entryDate = elementValue($event)"
          ></mdui-text-field>
          <template v-if="entryKind === 'busy_block'">
            <mdui-text-field
              label="开始"
              type="time"
              required
              :value="entryStart"
              @input="entryStart = elementValue($event)"
            ></mdui-text-field>
            <mdui-text-field
              label="结束日期"
              type="date"
              required
              :value="entryEndDate"
              @input="entryEndDate = elementValue($event)"
            ></mdui-text-field>
            <mdui-text-field
              label="结束"
              type="time"
              required
              :value="entryEnd"
              @input="entryEnd = elementValue($event)"
            ></mdui-text-field>
          </template>
          <label class="schedule-timezone-field">时区<TimeZoneSelect v-model="timezone" /></label>
          <div class="schedule-editor-actions">
            <mdui-button type="submit" variant="filled" :loading="busy" :disabled="busy || !entryTitle.trim()">
              {{ editingEntry ? '保存修改' : '保存安排' }}
            </mdui-button>
            <mdui-button type="button" variant="text" @click="stopEditingEntry">取消</mdui-button>
            <mdui-button v-if="pendingConflict" type="button" variant="outlined" :disabled="busy" @click="submitEntry(true)">
              保留重叠安排
            </mdui-button>
          </div>
        </form>
      </mdui-collapse-item>
    </mdui-collapse>

    <mdui-dialog
      class="schedule-detail-dialog"
      :open="Boolean(selectedItem)"
      close-on-overlay-click
      close-on-esc
      @closed="selectedItem = null"
    >
      <div v-if="selectedItem" class="schedule-item-sheet" aria-labelledby="selected-item-heading">
        <div class="schedule-item-sheet-heading">
          <div>
            <p class="eyebrow">{{ selectedItem.item_type === 'course' ? '课程' : '个人安排' }}</p>
            <h2 id="selected-item-heading">{{ selectedItem.title }}</h2>
            <p>{{ selectedItem.local_date }} · {{ selectedItemTime }}<template v-if="itemMeta(selectedItem)"> · {{ itemMeta(selectedItem) }}</template></p>
          </div>
          <mdui-button-icon aria-label="关闭详情" @click="selectedItem = null">
            <AppIcon name="close"></AppIcon>
          </mdui-button-icon>
        </div>
        <div class="schedule-item-sheet-actions">
          <template v-if="selectedItem.item_type === 'course'">
            <mdui-button variant="outlined" :disabled="busy" @click="cancelOccurrence(selectedItem)">取消本次课程</mdui-button>
            <mdui-button variant="text" :disabled="busy" @click="openReschedule(selectedItem)">改期本次</mdui-button>
            <mdui-button variant="text" :disabled="busy" @click="openMakeup(selectedItem)">安排补课</mdui-button>
            <mdui-button variant="text" :disabled="busy || detailLoading" @click="openEditCourseSeries(selectedItem)">编辑课程系列</mdui-button>
            <mdui-button variant="text" :disabled="busy || detailLoading" @click="archiveSelectedCourse">归档整门课程</mdui-button>
          </template>
          <template v-else>
            <mdui-button variant="filled" :disabled="busy" @click="editEntry(selectedItem)">编辑安排</mdui-button>
            <mdui-button variant="text" :disabled="busy" @click="archiveEntry(selectedItem)">归档安排</mdui-button>
          </template>
        </div>
        <div v-if="selectedItem.item_type === 'course'" class="schedule-linked-context">
          <p class="eyebrow">关联依据</p>
          <p v-if="detailLoading" class="helper-text">正在读取课程关联……</p>
          <template v-else-if="courseDetail?.links.length">
            <RouterLink
              v-for="link in courseDetail.links"
              :key="link.id"
              :to="linkDestination(link)"
              class="schedule-linked-row"
            >
              <span>{{ link.from_type === 'notice' || link.to_type === 'notice' ? '通知' : '任务' }}</span>
              <strong>{{ link.from_type === 'course_occurrence' ? link.to_title : link.from_title }}</strong>
              <small>{{ link.relation }}</small>
            </RouterLink>
          </template>
          <p v-else class="helper-text">这次课程暂时没有关联通知或任务；Agent 处理相关通知后会在这里留下依据。</p>
          <template v-if="courseDetail?.operations.length">
            <strong class="schedule-operation-title">最近操作</strong>
            <p v-for="operation in courseDetail.operations.slice(0, 3)" :key="operation.id" class="helper-text">
              {{ operation.summary }}
            </p>
          </template>
        </div>
      </div>
    </mdui-dialog>

    <section v-if="schedule?.conflicts.length" class="schedule-conflicts" aria-labelledby="schedule-conflicts-heading">
      <h2 id="schedule-conflicts-heading">时间冲突</h2>
      <p v-for="(conflict, index) in schedule.conflicts" :key="index">
        {{ conflict.local_date }} · {{ conflict.first.title }} / {{ conflict.second.title }} · {{ formatClock(conflict.start_at) }}–{{ formatClock(conflict.end_at) }}
      </p>
    </section>

    <mdui-dialog
      class="schedule-dialog"
      :open="Boolean(rescheduleItem)"
      close-on-overlay-click
      close-on-esc
      @closed="rescheduleItem = null"
    >
      <div v-if="rescheduleItem" class="schedule-card-content">
        <h2 id="reschedule-heading">改期本次课程：{{ rescheduleItem.title }}</h2>
        <label>新日期<input v-model="replacementDate" type="date" required /></label>
        <label>开始<input v-model="replacementStart" type="time" required /></label>
        <label>结束<input v-model="replacementEnd" type="time" required /></label>
        <label>地点<input v-model="replacementRoom" maxlength="500" /></label>
        <div class="day-actions">
          <mdui-button variant="filled" :disabled="busy" @click="submitReschedule">保存单次改期</mdui-button>
          <mdui-button variant="text" @click="rescheduleItem = null">取消</mdui-button>
        </div>
      </div>
    </mdui-dialog>

    <mdui-dialog
      class="schedule-dialog"
      :open="Boolean(makeupItem)"
      close-on-overlay-click
      close-on-esc
      @closed="makeupItem = null"
    >
      <div v-if="makeupItem" class="schedule-card-content">
        <h2 id="makeup-heading">安排补课：{{ makeupItem.title }}</h2>
        <label>日期<input v-model="makeupDate" type="date" required /></label>
        <label>开始<input v-model="makeupStart" type="time" required /></label>
        <label>结束<input v-model="makeupEnd" type="time" required /></label>
        <label>地点<input v-model="makeupRoom" maxlength="500" /></label>
        <label>原因<input v-model="makeupReason" maxlength="500" required /></label>
        <div class="day-actions">
          <mdui-button variant="filled" :disabled="busy" @click="submitMakeup">创建独立补课</mdui-button>
          <mdui-button variant="text" @click="makeupItem = null">取消</mdui-button>
        </div>
      </div>
    </mdui-dialog>

    <mdui-dialog
      class="schedule-dialog course-series-dialog"
      :open="courseEditorOpen"
      close-on-overlay-click
      close-on-esc
      @closed="courseEditorOpen = false"
    >
      <div v-if="courseEditorOpen" class="schedule-card-content">
          <p class="eyebrow">{{ editingSeriesId ? '修改整门课程' : '手动新增课程' }}</p>
          <h2 id="course-series-heading">{{ editingSeriesId ? `编辑：${formCourseTitle || '课程系列'}` : '新建常规课程' }}</h2>
          <p class="helper-text">
            {{ editingSeriesId
              ? '保存后会重建未受单次例外保护的课程实例；已取消、改期或补课记录不会被覆盖。'
              : '按星期和教学周生成常规课程实例。单次调课或补课仍请在课程详情中操作。' }}
          </p>
          <label>课程名称<input v-model="formCourseTitle" maxlength="500" required placeholder="例如：计算机组成原理" /></label>
          <div class="course-series-form-grid">
            <label>任课教师<input v-model="formCourseTeachers" maxlength="500" placeholder="多人用顿号分隔" /></label>
            <label>上课地点<input v-model="formCourseRoom" maxlength="500" placeholder="例如：实验楼 C308" /></label>
            <label>星期
              <select v-model.number="formCourseWeekday">
                <option :value="1">周一</option>
                <option :value="2">周二</option>
                <option :value="3">周三</option>
                <option :value="4">周四</option>
                <option :value="5">周五</option>
                <option :value="6">周六</option>
                <option :value="7">周日</option>
              </select>
            </label>
            <label>开始节次<input v-model.number="formCourseStartPeriod" type="number" min="1" max="30" @change="syncCourseTimes" /></label>
            <label>结束节次<input v-model.number="formCourseEndPeriod" type="number" min="1" max="30" @change="syncCourseTimes" /></label>
            <label>开始时间<input v-model="formCourseStartTime" type="time" required @input="formCourseUseActualTime = true" /></label>
            <label>结束时间<input v-model="formCourseEndTime" type="time" required @input="formCourseUseActualTime = true" /></label>
            <label>开始周<input v-model.number="formCourseStartWeek" type="number" min="1" :max="currentTerm?.week_count ?? 30" /></label>
            <label>结束周<input v-model.number="formCourseEndWeek" type="number" min="1" :max="currentTerm?.week_count ?? 30" /></label>
          </div>
          <p class="helper-text">{{ formCourseUseActualTime ? '使用课程真实起止时间。更改小节会重新匹配作息，无法推导时请重新填写。' : '当前时间按节次作息联动，可直接修改为课程真实起止时间。' }}</p>
          <p class="helper-text">
            当前学期：{{ currentTerm?.term_code ?? '未识别' }} · 第 {{ formCourseStartWeek }}–{{ formCourseEndWeek }} 周，每周{{ ['一','二','三','四','五','六','日'][formCourseWeekday - 1] }}。
          </p>
          <div class="day-actions">
            <mdui-button variant="filled" :disabled="busy || !formCourseTitle.trim()" @click="submitCourseSeries">
              {{ busy ? '保存中…' : editingSeriesId ? '保存课程系列' : '创建课程' }}
            </mdui-button>
            <mdui-button variant="text" :disabled="busy" @click="courseEditorOpen = false">取消</mdui-button>
          </div>
      </div>
    </mdui-dialog>

    <div v-if="lastException || lastArchivedEntry || lastCourseChange" class="undo-strip">
      <span>本次操作可撤销。</span>
      <mdui-button v-if="lastException" variant="outlined" :disabled="busy" @click="undoException">撤销课程例外</mdui-button>
      <mdui-button v-if="lastArchivedEntry" variant="outlined" :disabled="busy" @click="restoreLastEntry">恢复归档安排</mdui-button>
      <mdui-button v-if="lastCourseChange" variant="outlined" :disabled="busy" @click="undoLastCourseChange">
        撤销{{ lastCourseChange.label }}
      </mdui-button>
    </div>
  </section>
</template>
