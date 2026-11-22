import { apiRequest } from './client';

export interface CourseDraft {
  start_time?: string | null;
  end_time?: string | null;
  title: string;
  display_name: string;
  teachers: string[];
  room: string | null;
  week_expression: string;
  week_set: number[];
  raw_text: string;
  warnings: string[];
  weekday: number;
  periods: number[];
  source_cell: string;
}

export interface PeriodGroup {
  periods: number[];
  start_time: string | null;
  end_time: string | null;
  source_label: string;
}

export interface TimetableDraft {
  term_code: string | null;
  week_count: number;
  timezone: string;
  week1_monday: string | null;
  anchor_source: string;
  courses: CourseDraft[];
  period_profile: PeriodGroup[];
  unrecognized: Array<{ cell: string; reason: string }>;
  warnings: string[];
  metrics: {
    course_start_count: number;
    merged_range_count: number;
    weekday_count: number;
    sunday_course_count: number;
  };
}

export interface TimetableImport {
  id: string;
  term_code: string;
  file_format: 'xls' | 'xlsx';
  status: string;
  apply_mode: string | null;
  draft: TimetableDraft;
  diff: {
    added: Array<Record<string, unknown>>;
    changed: Array<Record<string, unknown>>;
    removed: Array<Record<string, unknown>>;
    unchanged: Array<Record<string, unknown>>;
    summary: Record<string, number>;
  };
  version: number;
  created_at_epoch: number;
  confirmed_at_epoch: number | null;
}

export interface ScheduleItem {
  id: string;
  course_series_id?: string;
  item_type: 'course' | 'personal';
  title: string;
  local_date: string;
  start_at: string | null;
  end_at: string | null;
  busy_segments: Array<{ start_at: string; end_at: string }>;
  weekday?: number;
  week_number?: number;
  teachers?: string[];
  room?: string | null;
  periods?: number[];
  term_code?: string;
  version: number;
  kind?: 'busy_block' | 'all_day' | 'deadline_marker';
  status?: string;
  timezone?: string;
  original_start_at?: string;
  original_end_at?: string;
}

export interface ScheduleResponse {
  start_date: string;
  end_date: string;
  terms: Array<{
    id: string;
    term_code: string;
    week1_monday: string;
    week_count: number;
    timezone: string;
    period_profile: PeriodGroup[];
    anchor_source: string;
    version: number;
  }>;
  items: ScheduleItem[];
  deadlines: Array<{ id: string; title: string; local_date: string; precision: string; raw: string }>;
  conflicts: Array<{ local_date: string; first: { id: string; title: string }; second: { id: string; title: string }; start_at: string; end_at: string }>;
}

export interface ManagedCourseSeries {
  start_time?: string | null;
  end_time?: string | null;
  id: string;
  term_id: string;
  term_code: string | null;
  title: string;
  display_name: string;
  teachers: string[];
  room: string | null;
  weekday: number;
  periods: number[];
  weeks: number[];
  status: 'active' | 'archived';
  version: number;
  source_import_id: string;
  occurrence_count: number | null;
  updated_at_epoch: number;
}

export interface CourseSeriesPayload {
  start_time?: string | null;
  end_time?: string | null;
  title: string;
  display_name?: string;
  teachers: string[];
  room?: string | null;
  weekday: number;
  periods: number[];
  week_numbers: number[];
  raw_text?: string;
  term_id?: string;
}

export interface CourseSeriesMutation {
  series: ManagedCourseSeries;
  change_set: { id: string; version: number; status: string; summary: string };
  conflicts: Array<ScheduleWriteConflict & { week_number?: number; local_date?: string }>;
}

// 写入时返回单个已占用对象，结构不同于周视图中的成对冲突。
export interface ScheduleWriteConflict {
  object_type: string;
  object_id: string;
  title: string;
  start_at: string;
  end_at: string;
}

export function parseScheduleWriteConflicts(value: unknown): ScheduleWriteConflict[] {
  // 异常响应也必须验证，防止未知数据导致整个日程页面渲染失败。
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is ScheduleWriteConflict => (
    item !== null && typeof item === 'object'
    && ['object_type', 'object_id', 'title', 'start_at', 'end_at'].every((key) => typeof item[key] === 'string')
    && Number.isFinite(Date.parse(item.start_at)) && Number.isFinite(Date.parse(item.end_at))
  ));
}

export interface CourseOverride {
  start_time?: string | null;
  end_time?: string | null;
  source_cell: string;
  title?: string;
  display_name?: string;
  teachers?: string[];
  room?: string | null;
  week_expression?: string;
}

export interface ScheduleEntryPayload {
  title: string;
  kind: 'busy_block' | 'all_day' | 'deadline_marker';
  local_date: string;
  start_at?: string;
  end_at?: string;
  timezone: string;
  allow_conflict?: boolean;
}

export async function previewTimetableImport(
  file: File,
  config: { week1_monday?: string; week_count: number; timezone: string; term_code?: string },
): Promise<{ import: TimetableImport; already_seen: boolean }> {
  const body = new FormData();
  body.set('file', file);
  if (config.week1_monday) body.set('week1_monday', config.week1_monday);
  body.set('week_count', String(config.week_count));
  body.set('timezone', config.timezone);
  if (config.term_code) body.set('term_code', config.term_code);
  return apiRequest('/api/v1/timetable/imports/preview', { method: 'POST', body });
}

export async function confirmTimetableImport(
  item: TimetableImport,
  input: {
    week1_monday: string;
    week_count: number;
    timezone: string;
    anchor_confirmed: boolean;
    apply_mode: 'merge' | 'replace_term';
    period_profile: PeriodGroup[];
    course_overrides: CourseOverride[];
  },
): Promise<TimetableImport> {
  return apiRequest(`/api/v1/timetable/imports/${item.id}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: item.version, ...input }),
  });
}

export async function listSchedule(startDate: string, endDate: string): Promise<ScheduleResponse> {
  const query = new URLSearchParams({ start_date: startDate, end_date: endDate });
  return apiRequest(`/api/v1/schedule?${query.toString()}`);
}

/** 全年查询拆为至多六段，保持后端原有 63 天上限与权限规则。 */
export async function listScheduleYear(year: number): Promise<ScheduleResponse> {
  const start = Date.UTC(year, 0, 1);
  const end = Date.UTC(year, 11, 31);
  const day = 86_400_000;
  const requests: Promise<ScheduleResponse>[] = [];
  for (let cursor = start; cursor <= end; cursor += 63 * day) {
    requests.push(listSchedule(new Date(cursor).toISOString().slice(0, 10), new Date(Math.min(end, cursor + 62 * day)).toISOString().slice(0, 10)));
  }
  const parts = await Promise.all(requests);
  return { start_date: `${year}-01-01`, end_date: `${year}-12-31`,
    terms: [...new Map(parts.flatMap(part => part.terms).map(term => [term.id, term])).values()],
    items: parts.flatMap(part => part.items), deadlines: parts.flatMap(part => part.deadlines), conflicts: parts.flatMap(part => part.conflicts) };
}

export async function createScheduleEntry(payload: ScheduleEntryPayload): Promise<{
  entry: ScheduleItem;
  conflicts: ScheduleWriteConflict[];
}> {
  return apiRequest('/api/v1/schedule', { method: 'POST', body: JSON.stringify(payload) });
}

export async function archiveScheduleEntry(item: ScheduleItem): Promise<ScheduleItem> {
  return apiRequest(`/api/v1/schedule/${item.id}/archive`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: item.version }),
  });
}

export async function updateScheduleEntry(item: ScheduleItem, payload: ScheduleEntryPayload): Promise<{
  entry: ScheduleItem;
  conflicts: ScheduleWriteConflict[];
}> {
  return apiRequest(`/api/v1/schedule/${item.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ expected_version: item.version, ...payload }),
  });
}

export async function restoreScheduleEntry(item: ScheduleItem): Promise<{
  entry: ScheduleItem;
  conflicts: ScheduleWriteConflict[];
}> {
  return apiRequest(`/api/v1/schedule/${item.id}/restore`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: item.version }),
  });
}

export async function applyCourseException(
  item: ScheduleItem,
  kind: 'cancel' | 'reschedule',
  reason: string,
  replacement?: { start_at: string; end_at: string; room?: string },
): Promise<{ exception: { id: string; version: number }; occurrence: ScheduleItem }> {
  return apiRequest(`/api/v1/timetable/occurrences/${item.id}/exceptions`, {
    method: 'POST',
    body: JSON.stringify({
      expected_version: item.version,
      kind,
      reason,
      ...(replacement
        ? {
            replacement_start: replacement.start_at,
            replacement_end: replacement.end_at,
            ...(replacement.room ? { replacement_room: replacement.room } : {}),
          }
        : {}),
    }),
  });
}

export async function undoCourseException(exceptionId: string, version: number): Promise<void> {
  await apiRequest(`/api/v1/course-exceptions/${exceptionId}/undo`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: version }),
  });
}

export async function createCourseMakeup(
  item: ScheduleItem,
  input: { local_date: string; start_at: string; end_at: string; room?: string; reason: string },
): Promise<{ occurrence: ScheduleItem; change_set: { id: string; version: number }; already_exists?: boolean }> {
  return apiRequest(`/api/v1/timetable/series/${item.course_series_id ?? item.id}/makeup`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
}

export async function listCourseSeries(
  input: { termId?: string; status?: 'active' | 'archived' | 'all'; query?: string } = {},
): Promise<{ items: ManagedCourseSeries[] }> {
  const query = new URLSearchParams();
  if (input.termId) query.set('term_id', input.termId);
  if (input.status) query.set('status', input.status);
  if (input.query?.trim()) query.set('q', input.query.trim());
  const suffix = query.size ? `?${query.toString()}` : '';
  return apiRequest(`/api/v1/timetable/series${suffix}`);
}

export async function createCourseSeries(payload: CourseSeriesPayload): Promise<CourseSeriesMutation> {
  return apiRequest('/api/v1/timetable/series', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export async function updateCourseSeries(
  seriesId: string,
  expectedVersion: number,
  payload: CourseSeriesPayload,
): Promise<CourseSeriesMutation> {
  return apiRequest(`/api/v1/timetable/series/${seriesId}`, {
    method: 'PATCH',
    body: JSON.stringify({ expected_version: expectedVersion, ...payload }),
  });
}

export async function archiveCourseSeries(seriesId: string, expectedVersion: number): Promise<CourseSeriesMutation> {
  return apiRequest(`/api/v1/timetable/series/${seriesId}/archive`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export async function restoreCourseSeries(seriesId: string, expectedVersion: number): Promise<CourseSeriesMutation> {
  return apiRequest(`/api/v1/timetable/series/${seriesId}/restore`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}
