/** 办理路线使用服务端版本保护；私人通知证据只返回给发起人。 */
import { apiRequest } from './client';
import type { EventSummary } from './business';

export interface Availability {
  timezone: string;
  start_time: string;
  end_time: string;
  weekdays: number[];
}
export interface JourneyStep {
  id: string;
  title: string;
  depends_on: string[];
  estimate_minutes: number | null;
  assigned_user_id: string;
  status: string;
  assignee_label?: string;
  source_removed?: boolean;
  schedule_ids?: string[];
}
export interface Journey {
  id: string;
  title: string;
  is_owner: boolean;
  version: number;
  task_id?: string;
  task_status?: string;
  steps: JourneyStep[];
  deadline?: EventSummary['deadline'];
  source_changed?: boolean;
  source?: { actor: string | null; stage: string | null; submission: string | null; unknowns: string[];
    evidence: { segment_id: string; quote: string }[]; notice_id: string | null;
    materials_total: number; unlisted_materials: string[] } | null;
  shared_title?: string | null;
  invite_active?: boolean;
  members: { user_id: string; alias: string; is_owner: boolean }[];
}
export interface StudentIntent {
  version: number;
  interests: string[];
  facts: string;
  availability: Availability;
}
export interface JourneyPlan {
  status: string;
  slots: { step_id: string; title: string; start_at: string; end_at: string; minutes: number }[];
  unresolved: { step_id: string; title: string; reason: string }[];
  required_minutes: number;
  available_minutes: number;
  already_booked_minutes: number;
  latest_start: string | null;
  horizon_end: string;
  assumptions: string[];
  plan_hash: string;
}
export interface Opportunity {
  event_id: string;
  version: number;
  title: string;
  notice_id: string | null;
  matched_interests: string[];
  scope: string | null;
  actor: string | null;
  stage: string | null;
  deadline: EventSummary['deadline'];
  materials: string[];
  submission: string | null;
  risks: string[];
  conflicts: string[];
  reason: string;
}

function send<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  return apiRequest(path, { method, body: JSON.stringify(body) });
}
export const listJourneys = () => apiRequest<{ items: Journey[]; total: number; limit: number }>('/api/v1/journeys');
export const getJourney = (id: string) => apiRequest<Journey>(`/api/v1/journeys/${id}`);
export const createJourney = (taskId: string) => send<Journey>(`/api/v1/tasks/${taskId}/journey`, {});
export const saveJourney = (route: Journey, steps: JourneyStep[]) => send<Journey>(`/api/v1/journeys/${route.id}`, {
  expected_version: route.version,
  steps: steps.map(({ id, title, depends_on, estimate_minutes, assigned_user_id }) => ({ id, title, depends_on, estimate_minutes, assigned_user_id })),
}, 'PUT');
export const refreshJourneySource = (route: Journey) => send<Journey>(`/api/v1/journeys/${route.id}/refresh-source`, { expected_version: route.version });
export const actOnStep = (route: Journey, step: JourneyStep) => send<Journey>(`/api/v1/journeys/${route.id}/steps/${step.id}`, {
  expected_version: route.version, action: step.status === 'done' ? 'reopen' : 'complete',
});
export const inviteJourney = (route: Journey, title: string) => send<{ journey: Journey; code: string }>(`/api/v1/journeys/${route.id}/invite`, {
  expected_version: route.version, shared_title: title, share_steps: true,
});
export const closeJourneyInvite = (route: Journey) => send<Journey>(`/api/v1/journeys/${route.id}/close-invite`, { expected_version: route.version });
export const joinJourney = (code: string, alias: string) => send<Journey>('/api/v1/journeys/join', { code, alias });
export const leaveJourney = (route: Journey) => send<{ left: boolean }>(`/api/v1/journeys/${route.id}/leave`, { expected_version: route.version });
export const revokeJourneyMember = (route: Journey, id: string) => send<Journey>(`/api/v1/journeys/${route.id}/members/${id}/revoke`, { expected_version: route.version });
export const previewJourneyPlan = (route: Journey, availability: Availability) => send<JourneyPlan>(`/api/v1/journeys/${route.id}/plan`, { expected_version: route.version, availability });
export const scheduleJourney = (route: Journey, availability: Availability, plan: JourneyPlan) => send<{ journey: Journey; plan: JourneyPlan }>(`/api/v1/journeys/${route.id}/schedule`, {
  expected_version: route.version, availability, plan_hash: plan.plan_hash,
});
export const getStudentIntent = () => apiRequest<StudentIntent>('/api/v1/student-intent');
export const saveStudentIntent = (intent: StudentIntent) => send<StudentIntent>('/api/v1/student-intent', {
  expected_version: intent.version, interests: intent.interests, facts: intent.facts, availability: intent.availability,
}, 'PUT');
export const listOpportunities = () => apiRequest<{ items: Opportunity[]; scope_note: string; scanned: number; total_events: number }>('/api/v1/opportunities');
export const adoptOpportunity = (opportunity: Opportunity) => send<Journey>(`/api/v1/opportunities/${opportunity.event_id}/adopt`, { expected_version: opportunity.version });
