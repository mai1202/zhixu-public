// 新能力使用独立接口，不改变采集端共享的 NormalizedBundle 1.0。
import { apiRequest } from './client';

export interface RequirementState {
  event_id: string; version: number; field_policies: Record<string, boolean>;
  current: Record<string, unknown>;
  claims: Array<{ id: string; field: string; value: unknown; status: string; source_id: string; basis_kind?: string;
    evidence_check?: { status: string; reason: string; scope_checked: boolean } | null; evidence: Array<{ quote: string }> }>;
}
export interface PlanningResult {
  id: string; status: string; input_hash: string; change_set_id?: string;
  result: { status: string; strategy: string; moved_count: number; moved_minutes: number;
    slots: Array<{ id: string; title: string; start_at: string; end_at: string }>;
    unresolved: Array<{ id: string; reason: string }>; assumptions: string[];
    // 兼容历史方案；诊断是可验证的约束说明，不表示概率或全局最优证明。
    comparison?: Array<{ strategy: string; unresolved_count: number; moved_count: number; moved_minutes: number }>;
    diagnostics?: { note: string; certificates: Array<{ kind: string; reason: string; proven: boolean; unit_ids: string[]; start_at?: string; end_at?: string }>;
      units: Array<{ id: string; title: string; critical_path_minutes: number; latest_start_bound: string }> } };
}
export interface PlanningPreferences {
  version: number; automatic: boolean; automatic_available: boolean; freeze_minutes: number;
  journey_ids: string[];
  availability: { timezone: string; start_time: string; end_time: string; weekdays: number[] };
}
export const getRequirements = (id: string) => apiRequest<RequirementState>(`/api/v1/events/${id}/requirements`);
export const getPlanningPreferences = () => apiRequest<PlanningPreferences>('/api/v1/planning/preferences');
export const savePlanningPreferences = (body: unknown) => apiRequest<PlanningPreferences>('/api/v1/planning/preferences', { method: 'PUT', body: JSON.stringify(body) });
export const previewPlanning = (body: unknown) => apiRequest<PlanningResult>('/api/v1/planning/preview', { method: 'POST', body: JSON.stringify(body) });
export const getPlanningRun = (id: string) => apiRequest<PlanningResult>(`/api/v1/planning/runs/${id}`);
export const applyPlanning = (run: PlanningResult) => apiRequest<PlanningResult>(`/api/v1/planning/runs/${run.id}/apply`, { method: 'POST', body: JSON.stringify({ input_hash: run.input_hash }) });
export interface TodayActions {
  items: Array<{ id: string; kind: string; title: string; reason: string; href: string; start_at?: string }>;
  changes: Array<{ id: string; summary: string; status: string; change_set_id: string }>;
}
export const getTodayActions = () => apiRequest<TodayActions>('/api/v1/today/actions');
export interface SourceHealth {
  devices: Array<{ id: string; name: string; status: string; last_success: number | null }>;
  notification_processing: { pending: number; failed: number };
  attachments: Record<string, number>;
  watches: Array<{ id: string; status: string; last_success: number | null }>;
  mail: { status: string; note: string; poll_status: string; last_success: number | null }; coverage_note: string;
}
export const getSourceHealth = () => apiRequest<SourceHealth>('/api/v1/sources/health');
