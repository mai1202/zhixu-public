import { apiRequest } from './client';

// 平台额度由服务端按实际账户核算，自配密钥与豁免账户不受此额度限制。
export interface PlatformQuota {
  day: string; timezone: string; resets_at: string; exempt: boolean;
  limit: number | null; used: number; reserved: number; remaining: number | null; self_configured_excluded: boolean;
  currency: 'CNY'; limit_cny: number | null; spent_cny: number; reserved_cny: number; remaining_cny: number | null;
}
export const getPlatformQuota = () => apiRequest<PlatformQuota>('/api/v1/models/platform-quota');

// 人民币账单来自同一逐请求账本，不采用 SDK 的美元参考价格。
export interface BillingModel {
  profile_id: string; label: string; model_id: string; platform_paid: boolean;
  spent_cny: number; reserved_cny: number; unknown_calls: number; estimated_calls: number; calls: number;
  prompt_tokens: number; cache_hit_tokens: number; cache_miss_tokens: number; completion_tokens: number;
  pricing: { rates?: string[] | null; period?: string; source?: string; verified_at?: string; calendar_estimated?: boolean };
}
export interface ModelBilling { quota: PlatformQuota; models: BillingModel[]; note: string; pricing_source: string;
  recent: Array<{ id: string; profile_id: string; model_id: string; created_at_epoch: number; status: string; cost_cny: number | null; platform_paid: boolean }>;
}
export const getModelBilling = () => apiRequest<ModelBilling>('/api/v1/models/billing');

export interface ModelCatalogEntry {
  provider: string;
  label: string;
  configurable: boolean;
  deferred_reason: string | null;
  api_family: string;
  default_base_url: string | null;
  requires_key: boolean;
  capabilities: Record<string, string>;
}

export interface ProviderProfile {
  id: string;
  label: string;
  provider: string;
  api_family: string;
  purpose: 'extraction' | 'planning' | 'mail_suggestion';
  base_url: string;
  model_id: string;
  execution_target: 'server' | 'device';
  device_id: string | null;
  is_default: boolean;
  has_api_key: boolean;
  needs_data_consent: boolean;
  data_consent: boolean;
  capabilities: Record<string, string>;
  billing_prices?: { cache_hit: string; cache_miss: string; output: string } | null;
  timeout_seconds: number;
  max_output_tokens: number;
  enabled: boolean;
  fallback_policy: 'none' | 'rules';
  last_test_status: string;
  last_test_epoch: number | null;
  version: number;
}

export interface ModelRun {
  id: string;
  task_kind: string;
  provider: string;
  model_id: string;
  status: string;
  failure_code: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  cost_usd: number | null;
  latency_ms: number;
  created_at_epoch: number;
}

export async function listModelCatalog(): Promise<{ items: ModelCatalogEntry[] }> {
  return apiRequest('/api/v1/models/catalog');
}

export async function listProviderProfiles(): Promise<{ items: ProviderProfile[] }> {
  return apiRequest('/api/v1/models/profiles');
}

export async function createProviderProfile(payload: {
  label: string;
  provider: string;
  purpose: ProviderProfile['purpose'];
  base_url?: string;
  model_id: string;
  execution_target: 'server' | 'device';
  device_id?: string;
  api_key?: string;
  timeout_seconds: number;
  max_output_tokens: number;
  fallback_policy: ProviderProfile['fallback_policy'];
  billing_prices?: { cache_hit: string; cache_miss: string; output: string };
}): Promise<ProviderProfile> {
  return apiRequest('/api/v1/models/profiles', { method: 'POST', body: JSON.stringify(payload) });
}

export async function updateProviderProfile(
  profile: ProviderProfile,
  updates: { enabled?: boolean; api_key?: string; billing_prices?: ProviderProfile['billing_prices'] },
): Promise<ProviderProfile> {
  return apiRequest(`/api/v1/models/profiles/${profile.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ expected_version: profile.version, ...updates }),
  });
}

export async function deleteProviderProfile(profile: ProviderProfile): Promise<void> {
  await apiRequest(`/api/v1/models/profiles/${profile.id}`, { method: 'DELETE' });
}

export async function testProviderProfile(profile: ProviderProfile): Promise<{
  status: string;
  job_id?: string;
  provider: string;
  model_id: string;
  capabilities: Record<string, string>;
  usage: { prompt_tokens: number | null; completion_tokens: number | null; cost_usd: number | null };
}> {
  return apiRequest(`/api/v1/models/profiles/${profile.id}/test`, { method: 'POST', body: '{}' });
}

export interface GenericJobStatus {
  id: string;
  kind: string;
  status: string;
  result: Record<string, unknown> | null;
  error_code: string | null;
}

export async function getModelJob(jobId: string): Promise<GenericJobStatus> {
  return apiRequest(`/api/v1/jobs/${jobId}`);
}

export async function listModelRuns(): Promise<{ items: ModelRun[] }> {
  return apiRequest('/api/v1/models/runs');
}
