import { apiRequest } from './client';

export type Mailbox = 'inbox' | 'archived' | 'ignored';

export interface NotificationCounts {
  inbox: number;
  archived: number;
  ignored: number;
  unread: number;
}

export interface NotificationMessage {
  id: string;
  title: string;
  summary: string | null;
  advice: string | null;
  organized: boolean;
  mailbox: Mailbox;
  read: boolean;
  version: number;
  created_at_epoch: number;
  source_type: string;
  original_text?: string;
  original_title?: string;
  source_count?: number;
  sources?: Array<{ id: string; title: string; original_text: string; created_at_epoch: number }>;
  change_set?: { id: string; summary: string } | null;
  notices?: Array<{ id: string; title: string; status: string }>;
  notices_has_more?: boolean;
}

export async function listNotifications(mailbox: Mailbox, query: string, offset = 0): Promise<{ items: NotificationMessage[]; has_more: boolean }> {
  const params = new URLSearchParams({ mailbox, q: query, offset: String(offset) });
  return apiRequest(`/api/v1/notifications?${params}`);
}

export async function getNotificationCounts(): Promise<NotificationCounts> {
  return apiRequest('/api/v1/notifications/counts');
}

export async function getNotification(id: string): Promise<NotificationMessage> {
  return apiRequest(`/api/v1/notifications/${id}`);
}

export async function updateNotification(
  id: string,
  patch: { read?: boolean; mailbox?: Mailbox },
  expectedVersion: number,
): Promise<NotificationMessage> {
  return apiRequest(`/api/v1/notifications/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ ...patch, expected_version: expectedVersion }),
  });
}
