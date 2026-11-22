import { apiRequest } from './client';

export interface VerifiedMailAddress {
  id: string;
  email: string;
  verified_at_epoch: number;
  is_primary: boolean;
}

export interface DigestContentOptions {
  courses: boolean;
  arrangements: boolean;
  deadlines: boolean;
  changes: boolean;
  tomorrow: boolean;
  agent_summary: boolean;
  suggestions: boolean;
}

export interface DigestPreferences {
  id: string;
  enabled: boolean;
  timezone: string;
  send_time: string;
  recipient_address_id: string | null;
  recipient: string | null;
  include: DigestContentOptions;
  weekly_enabled: boolean;
  weekly_send_day: number;
  weekly_send_time: string;
  next_weekly_send_epoch: number | null;
  reminder_enabled: boolean;
  reminder_offsets: number[];
  notification_flags: Record<string, boolean>;
  quiet_hours: { enabled: boolean; start: string; end: string };
  merge_window_minutes: number;
  version: number;
  next_send_epoch: number | null;
  addresses: VerifiedMailAddress[];
}

export interface MailOutboxItem {
  id: string;
  purpose: 'digest' | 'test' | string;
  template_name: string;
  recipient: string;
  subject: string;
  body_text: string;
  status: 'queued' | 'sending' | 'accepted' | 'failed' | 'unknown' | 'cancelled' | string;
  priority: number;
  attempt: number;
  accepted_at_epoch: number | null;
  error_code: string | null;
  created_at_epoch: number;
  updated_at_epoch: number;
}

export async function getMailStatus(): Promise<{ channel: string; status: string; delivery_claim: string }> {
  return apiRequest('/api/v1/mail/status');
}

export async function getDigestPreferences(): Promise<DigestPreferences> {
  return apiRequest('/api/v1/mail/preferences');
}

export async function updateDigestPreferences(input: {
  expected_version: number;
  enabled: boolean;
  timezone: string;
  send_time: string;
  recipient_address_id: string | null;
  include: DigestPreferences['include'];
  weekly_enabled?: boolean;
  weekly_send_day?: number;
  weekly_send_time?: string;
  reminder_enabled?: boolean;
  reminder_offsets?: number[];
  notification_flags?: Record<string, boolean>;
  quiet_hours?: DigestPreferences['quiet_hours'];
  merge_window_minutes?: number;
}): Promise<DigestPreferences> {
  return apiRequest('/api/v1/mail/preferences', { method: 'PATCH', body: JSON.stringify(input) });
}

export async function previewDigest(): Promise<{
  local_date: string;
  recipient: string | null;
  subject: string;
  body_text: string;
  change_seq_from: number;
  change_seq_through: number;
  delivery_status: string;
}> {
  return apiRequest('/api/v1/mail/preview');
}

export async function queueTestMail(): Promise<MailOutboxItem> {
  return apiRequest('/api/v1/mail/test', { method: 'POST', body: '{}' });
}

export async function listMailOutbox(): Promise<{ items: MailOutboxItem[] }> {
  return apiRequest('/api/v1/mail/outbox?limit=50');
}

export async function retryMail(id: string): Promise<MailOutboxItem> {
  return apiRequest(`/api/v1/mail/outbox/${id}/retry`, { method: 'POST', body: '{}' });
}
