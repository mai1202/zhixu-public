import type { NormalizedBundle } from '@notification-management/contracts';
import { apiRequest } from './client';

export interface SourceSummary {
  id: string;
  source_type: 'text' | 'manual' | string;
  title: string;
  original_text: string;
  analysis_state: string;
  analysis_mode?: 'rules' | 'llm' | 'auto' | string;
  provider_profile_id?: string | null;
  created_at_epoch: number;
}

export interface ChangeSetSummary {
  id: string;
  kind: string;
  status?: string;
  applied?: boolean;
  summary: string;
  bundle: NormalizedBundle | null;
  operations: Record<string, unknown>[];
  version: number;
  created_at_epoch: number;
  confirmed_at_epoch?: number | null;
  applied_at_epoch?: number | null;
  undone_at_epoch: number | null;
}

export interface InboxDetail {
  change_set: ChangeSetSummary;
  source: SourceSummary | null;
  ocr_artifact: { id: string; image_url: string } | null;
  segments: Array<{
    id: string;
    ordinal: number;
    text: string;
    start_offset: number;
    end_offset: number;
  }>;
}

export interface NoticeSummary {
  id: string;
  source_document_id: string;
  title: string;
  importance: string;
  last_change_set_id: string | null;
  status: string;
  version: number;
  created_at_epoch: number;
  updated_at_epoch: number;
}

export interface EventSummary {
  id: string;
  notice_id: string | null;
  source_document_id: string;
  title: string;
  actor: string | null;
  stage: string | null;
  scope: string | null;
  deadline: NormalizedBundle['events'][number]['deadline'];
  time_range: NormalizedBundle['events'][number]['time_range'];
  materials: string[];
  submission: string | null;
  eligibility: string;
  evidence: Record<string, Array<{ segment_id: string; quote: string }>>;
  unknowns: string[];
  status: string;
  version: number;
  updated_at_epoch: number;
}

export interface TaskSummary {
  id: string;
  event_id: string | null;
  source_document_id: string | null;
  notice_id: string | null;
  course_series_id: string | null;
  course_occurrence_id: string | null;
  title: string;
  description: string | null;
  task_kind: string;
  priority: string;
  due: EventSummary['deadline'];
  original_requirement: string | null;
  reminder_enabled: boolean;
  status: string;
  version: number;
  updated_at_epoch: number;
  completed_at_epoch: number | null;
  notice_title?: string | null;
  course_title?: string | null;
  course_date?: string | null;
}

export interface NoticeDetail {
  notice: NoticeSummary;
  source: SourceSummary;
  ocr_artifact: { id: string; image_url: string } | null;
  segments: InboxDetail['segments'];
  events: EventSummary[];
  tasks: TaskSummary[];
  links: Array<{
    id: string;
    from_type: string;
    from_id: string;
    to_type: string;
    to_id: string;
    relation: string;
    status: string;
    version: number;
  }>;
  courses: Array<{
    course_series_id: string;
    occurrence_id: string | null;
    title: string;
    local_date: string | null;
    start: string | null;
    end: string | null;
    room: string | null;
    relation: string;
  }>;
  operations: Array<{
    id: string;
    actor_type: string;
    action: string;
    summary: string;
    change_set_id: string | null;
    created_at_epoch: number;
  }>;
  revisions: Array<{
    version: number;
    data: Record<string, unknown>;
    reason: string;
    created_at_epoch: number;
  }>;
}

interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

function pageQuery(limit = 50, offset = 0): string {
  return new URLSearchParams({ limit: String(limit), offset: String(offset) }).toString();
}

export function listInbox(): Promise<Page<ChangeSetSummary>> {
  // 忽略和撤销仍保留原文；旧接口只读操作历史，不再提供核对状态筛选。
  return apiRequest(`/api/v1/inbox?${pageQuery()}`);
}

export function getInboxItem(id: string): Promise<InboxDetail> {
  return apiRequest(`/api/v1/inbox/${id}`);
}

export function createTextSource(
  text: string,
  title?: string,
  analysis?: { mode: 'rules' | 'llm' | 'auto'; providerProfileId?: string },
  idempotencyKey?: string,
): Promise<{
  source: SourceSummary;
  change_set: ChangeSetSummary;
  analysis_job_id?: string;
}> {
  return apiRequest('/api/v1/sources', {
    method: 'POST',
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    body: JSON.stringify({
      source_type: 'text',
      text,
      ...(title ? { title } : {}),
      ...(analysis ? { analysis_mode: analysis.mode, ...(analysis.providerProfileId ? { provider_profile_id: analysis.providerProfileId } : {}) } : {}),
    }),
  });
}

export function createManualSource(input: {
  title?: string;
  manual_event: {
    title: string;
    actor?: string;
    stage?: string;
    scope?: string;
    deadline_date?: string;
    deadline_time?: string;
    materials?: string[];
    submission?: string;
  };
}, idempotencyKey?: string): Promise<{ source: SourceSummary; change_set: ChangeSetSummary; analysis_job_id?: string }> {
  return apiRequest('/api/v1/sources', {
    method: 'POST',
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
    body: JSON.stringify({ source_type: 'manual', ...input }),
  });
}

export function updateCandidate(changeSet: ChangeSetSummary, bundle: NormalizedBundle): Promise<ChangeSetSummary> {
  return apiRequest(`/api/v1/changes/${changeSet.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ expected_version: changeSet.version, bundle }),
  });
}

export function confirmChange(changeSet: ChangeSetSummary): Promise<ChangeSetSummary> {
  return apiRequest(`/api/v1/changes/${changeSet.id}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: changeSet.version }),
  });
}

export function rejectChange(changeSet: ChangeSetSummary): Promise<ChangeSetSummary> {
  return apiRequest(`/api/v1/changes/${changeSet.id}/reject`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: changeSet.version }),
  });
}

export function undoChange(id: string): Promise<ChangeSetSummary> {
  return apiRequest(`/api/v1/changes/${id}/undo`, { method: 'POST', body: JSON.stringify({}) });
}

export function reopenChange(change: ChangeSetSummary): Promise<ChangeSetSummary> {
  return apiRequest(`/api/v1/changes/${change.id}/reopen`, { method: 'POST', body: JSON.stringify({ expected_version: change.version }) });
}

export function listNotices(query = ''): Promise<Page<NoticeSummary>> {
  const params = new URLSearchParams({ limit: '50', offset: '0' });
  if (query.trim()) params.set('q', query.trim());
  return apiRequest(`/api/v1/notices?${params.toString()}`);
}

export function getNotice(id: string): Promise<NoticeDetail> {
  return apiRequest(`/api/v1/notices/${id}`);
}

export function patchNotice(
  notice: NoticeSummary,
  input: { title?: string; importance?: string },
): Promise<{ notice: NoticeSummary; change_set: ChangeSetSummary }> {
  return apiRequest(`/api/v1/notices/${notice.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ expected_version: notice.version, ...input }),
  });
}

export function proposeArchiveNotice(id: string): Promise<{ change_set: ChangeSetSummary }> {
  return apiRequest(`/api/v1/notices/${id}/archive`, { method: 'POST', body: JSON.stringify({}) });
}

export function listTasks(status: 'open' | 'completed' | 'cancelled' = 'open', limit = 50): Promise<Page<TaskSummary>> {
  // 办理路线可显式读取较大一页，仍受服务端分页上限约束。
  return apiRequest(`/api/v1/tasks?${pageQuery(limit)}&status=${status}`);
}

export function setTaskStatus(task: TaskSummary, action: 'complete' | 'reopen'): Promise<{
  task: TaskSummary;
  change_set: ChangeSetSummary | null;
}> {
  return apiRequest(`/api/v1/tasks/${task.id}/${action}`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: task.version }),
  });
}

export function cancelTask(task: TaskSummary): Promise<{
  task: TaskSummary;
  change_set: ChangeSetSummary | null;
}> {
  return apiRequest(`/api/v1/tasks/${task.id}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: task.version }),
  });
}
