import { apiRequest } from './client';

export interface OnboardingStep {
  key: string; title: string; href: string; detail: string; status: 'done' | 'skipped' | 'pending';
}
export interface OnboardingState {
  status: string; steps: OnboardingStep[]; completed_count: number; step_count: number;
  ready_to_complete: boolean; model_ready: boolean; automatic_notifications: boolean;
  reminder_enabled: boolean; reminder_due_at_epoch: number; reminder_queued: boolean;
  reminder_delay_seconds: number;
  snoozed_until_epoch: number | null; show_prompt: boolean; skipped_sections: string[]; version: number;
}
export function getOnboarding(): Promise<OnboardingState> { return apiRequest('/api/v1/onboarding'); }
export function updateOnboarding(state: OnboardingState, patch: { action?: 'save' | 'complete' | 'snooze' | 'resume'; skipped_sections?: string[]; reminder_enabled?: boolean }): Promise<OnboardingState> {
  // 进度编辑也携带版本，防止多个页面相互覆盖“稍后设置”和邮件选择。
  return apiRequest('/api/v1/onboarding', { method: 'PATCH', body: JSON.stringify({ expected_version: state.version, ...patch }) });
}
export function startOnboarding(goals: string, key: string): Promise<{ conversation_id: string; run_id: string }> {
  return apiRequest('/api/v1/onboarding/start', { method: 'POST', body: JSON.stringify({ goals, idempotency_key: key }) });
}
