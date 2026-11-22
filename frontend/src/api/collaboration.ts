/** 协作请求使用统一认证；共享权限由后端逐次核验。 */
import { apiRequest } from './client';
import type { Availability } from './journeys';

export interface CollaborationContent {
  comments: { id: string; content: string; author: string; can_delete: boolean }[];
  files: { id: string; filename: string; size_bytes: number; audience: 'owner' | 'members'; author: string; can_delete: boolean; download_url: string }[];
}
export interface AvailabilityConsent {
  version: number; enabled: boolean; availability: Availability; expires_at_epoch: number | null;
}
export interface CommonAvailability {
  status: 'ready' | 'awaiting_consent'; participants: number; consented: number;
  slots: { start_at: string; end_at: string }[];
}
const base = (id: string) => `/api/v1/journeys/${id}`;
const send = <T>(path: string, body: unknown, method = 'POST') => apiRequest<T>(path, { method, body: JSON.stringify(body) });
export const getCollaboration = (id: string) => apiRequest<CollaborationContent>(`${base(id)}/collaboration`);
export const postComment = (id: string, content: string) => send<CollaborationContent>(`${base(id)}/comments`, { content });
export const submitFile = (id: string, attachment_id: string, audience: 'owner' | 'members') => send<CollaborationContent>(`${base(id)}/files`, { attachment_id, audience });
export const removeCollaboration = (id: string, kind: 'comments' | 'files', item: string) => apiRequest<CollaborationContent>(`${base(id)}/${kind}/${item}`, { method: 'DELETE' });
export const getAvailabilityConsent = (id: string) => apiRequest<AvailabilityConsent>(`${base(id)}/availability`);
export const saveAvailabilityConsent = (id: string, state: AvailabilityConsent, enabled: boolean) => send<AvailabilityConsent>(`${base(id)}/availability`, {
  // 撤回不依赖尚未保存的窗口草稿是否合法。
  expected_version: state.version, enabled, ...(enabled ? { availability: state.availability } : {}), valid_days: 7,
}, 'PUT');
export const getCommonAvailability = (id: string) => apiRequest<CommonAvailability>(`${base(id)}/common-availability`);
