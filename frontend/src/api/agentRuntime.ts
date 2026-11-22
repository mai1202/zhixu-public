import { apiRequest } from './client';

/** Agent 无法正常工作的原因；正常时 healthy=true 且界面不展示提示卡。 */
export type AgentRuntimeStatus =
  | 'ok'
  | 'no_profile'
  | 'quota_exhausted'
  | 'key_rejected'
  | 'rate_limited'
  | 'unavailable'
  | 'failing';

export interface AgentRuntimeHealth {
  status: AgentRuntimeStatus;
  healthy: boolean;
  title: string;
  detail: string;
  action_label: string;
  action_href: string;
  failure_label: string | null;
  last_failure_code: string | null;
  last_failure_epoch: number | null;
  checked_at_epoch: number;
  enabled_profiles: number;
  quota: {
    exempt: boolean;
    limit_cny: number | null;
    spent_cny: number;
    remaining_cny: number | null;
    resets_at: string;
  };
}

export const getAgentRuntimeHealth = () => apiRequest<AgentRuntimeHealth>('/api/v1/agent/runtime-health');
