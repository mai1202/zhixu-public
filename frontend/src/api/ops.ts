import { apiRequest } from './client';
import type { TaskSummary } from './business';

export interface OperationItem {
  id: string;
  actor_type: 'agent' | 'user' | 'system' | string;
  actor_id: string | null;
  action: string;
  object_type: string;
  object_id: string;
  change_set_id: string | null;
  source_document_id: string | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  summary: string;
  status: string;
  undoable: boolean;
  created_at_epoch: number;
}

export interface DashboardData {
  timezone: string;
  local_date: string;
  metrics: {
    notices_processed: number;
    notices_active: number;
    tasks_total: number;
    tasks_created: number;
    task_updates: number;
    tasks_open: number;
    tasks_overdue: number;
    tasks_due_7d: number;
    course_changes: number;
    deduplicated: number;
    reminders_sent: number;
    reminders_pending: number;
    reports_generated: number;
    mail_accepted: number;
    job_failures: number;
    average_processing_seconds: number | null;
    processing_sample_count: number;
    agent_actions: number;
    manual_interventions: number;
    automation_rate: number;
  };
  metric_definitions: Record<string, string>;
  agent_status: {
    automatic_notifications: boolean;
    model_ready: boolean;
    running_jobs: number;
    active_insights: number;
    last_action: OperationItem | null;
  };
  recent_operations: OperationItem[];
  mail: {
    daily_enabled: boolean;
    weekly_enabled: boolean;
    reminder_enabled: boolean;
    recipient_configured: boolean;
  };
  generated_at_epoch: number;
}

export interface ReportItem {
  id: string;
  kind: 'daily' | 'weekly';
  period_start: string;
  period_end: string;
  timezone: string;
  status: string;
  subject: string;
  body_text: string;
  sections: Record<string, unknown>;
  metadata: Record<string, unknown>;
  links: Array<{ type: string; id: string; title: string; href: string }>;
  outbox_id: string | null;
  generated_at_epoch: number;
  sent_at_epoch: number | null;
  version: number;
}

export interface CourseSearchItem {
  occurrence_id: string;
  course_series_id: string;
  title: string;
  teachers: string[];
  room: string | null;
  local_date: string;
  start: string;
  end: string;
  week_number: number;
  status: string;
  version: number;
}

export interface CourseDetail {
  occurrence: CourseSearchItem;
  series: {
    start_time?: string | null;
    end_time?: string | null;
    id: string;
    term_id: string;
    title: string;
    display_name: string;
    teachers: string[];
    weekday: number;
    periods: number[];
    weeks: number[];
    room: string | null;
    status: string;
    version: number;
  };
  links: Array<{
    id: string;
    from_type: string;
    from_id: string;
    to_type: string;
    to_id: string;
    relation: string;
    from_title?: string;
    to_title?: string;
    status: string;
  }>;
  operations: OperationItem[];
}

export interface ReminderItem {
  id: string;
  task_id: string | null;
  notice_id: string | null;
  kind: string;
  scheduled_at_epoch: number;
  offset_minutes: number | null;
  status: string;
  metadata: {
    title?: string;
    priority?: string;
    due?: { local_date?: string; raw?: string };
    note?: string;
  };
  outbox_id: string | null;
  sent_at_epoch: number | null;
  error_code: string | null;
}

export interface AgentProgress {
  status: string;
  error_code?: string | null;
  events: Array<{
    sequence: number;
    type: string;
    label: string;
    status: string;
  }>;
}

export interface TaskDetail {
  task: TaskSummary;
  event: Record<string, unknown> | null;
  links: Array<{
    id: string;
    from_type: string;
    from_id: string;
    to_type: string;
    to_id: string;
    relation: string;
    from_title?: string;
    to_title?: string;
    status: string;
  }>;
  operations: OperationItem[];
}

export function getDashboard(): Promise<DashboardData> {
  return apiRequest('/api/v1/dashboard');
}

export function listOperations(limit = 50): Promise<{ items: OperationItem[]; total: number }> {
  return apiRequest(`/api/v1/operations?limit=${limit}`);
}

export function listReports(limit = 30): Promise<{ items: ReportItem[] }> {
  return apiRequest(`/api/v1/reports?limit=${limit}`);
}

export function generateReport(input: {
  kind: 'daily' | 'weekly';
  period_start?: string;
  send?: boolean;
}): Promise<ReportItem | { report: ReportItem; outbox: { id: string; status: string } }> {
  return apiRequest('/api/v1/reports', { method: 'POST', body: JSON.stringify(input) });
}

export function sendReport(id: string): Promise<{ report: ReportItem; outbox: { id: string; status: string } }> {
  return apiRequest(`/api/v1/reports/${id}/send`, { method: 'POST', body: '{}' });
}

export function listReminders(limit = 50): Promise<{ items: ReminderItem[] }> {
  return apiRequest(`/api/v1/reminders?limit=${limit}`);
}

export function getAgentProgress(sourceId: string): Promise<AgentProgress> {
  return apiRequest(`/api/v1/sources/${sourceId}/agent-progress`);
}

export function searchCourses(query = '', dates?: { start?: string; end?: string }): Promise<{
  items: CourseSearchItem[];
  start_date: string;
  end_date: string;
}> {
  const params = new URLSearchParams({ q: query, limit: '50' });
  if (dates?.start) params.set('start_date', dates.start);
  if (dates?.end) params.set('end_date', dates.end);
  return apiRequest(`/api/v1/courses/search?${params.toString()}`);
}

export function getCourseDetail(occurrenceId: string): Promise<CourseDetail> {
  return apiRequest(`/api/v1/courses/occurrences/${occurrenceId}`);
}

export function getTaskDetail(taskId: string): Promise<TaskDetail> {
  return apiRequest(`/api/v1/tasks/${taskId}`);
}

export function patchTask(taskId: string, input: Record<string, unknown>): Promise<{
  task: TaskSummary;
  change_set: { id: string; version: number };
}> {
  return apiRequest(`/api/v1/tasks/${taskId}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
}

export function createDemoNotification(text: string, title?: string, idempotencyKey?: string): Promise<{
  source: { id: string; title: string };
  change_set: { id: string; summary: string; status: string };
  analysis_job_id?: string;
  demo: true;
}> {
  return apiRequest('/api/v1/demo/notifications', {
    method: 'POST',
    // 调用方可复用一次提交的标识；没有传入时仍创建独立演示请求。
    headers: { 'Idempotency-Key': idempotencyKey ?? crypto.randomUUID() },
    body: JSON.stringify({ text, ...(title ? { title } : {}), analysis_mode: 'auto' }),
  });
}

export function createManagedTask(input: {
  title: string;
  description?: string | null;
  task_kind?: string;
  priority?: string;
  deadline?: Record<string, unknown> | null;
  course_occurrence_id?: string | null;
  reminder_enabled?: boolean;
}): Promise<{ task: import('./business').TaskSummary; change_set: { id: string; version: number } }> {
  return apiRequest('/api/v1/tasks', {
    method: 'POST',
    body: JSON.stringify(input),
  });
}
