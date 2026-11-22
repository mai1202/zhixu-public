import { apiRequest } from './client';

export interface DeviceChat {
  chat_id: string;
  chat_name: string;
}

export interface DeviceSettings {
  available_chats: DeviceChat[];
  selected_chats: DeviceChat[];
  attachments_enabled: boolean;
  attachment_kinds?: Array<'image' | 'file'>;
  attachment_auto_notice?: boolean;
  chat_selection_mode?: 'all' | 'selected';
  excluded_chat_ids?: string[];
  sync_interval_seconds?: number;
  sync_request_id?: string;
  sync_ack_id?: string;
  sync_result?: 'pending' | 'running' | 'completed' | 'partial' | 'waiting_for_chat_selection';
  // 首次绑定的同步状态由客户端进度与回执驱动，不能用“没有消息”推断失败。
  initial_sync_status?: 'pending' | 'running' | 'completed' | 'partial' | 'waiting_for_chat_selection';
}

export interface PairedDevice {
  id: string;
  name: string;
  fingerprint_hint: string;
  status: 'pending' | 'active' | 'revoked';
  settings: DeviceSettings;
  version: number;
  last_heartbeat_epoch: number | null;
  last_collect_epoch: number | null;
  last_upload_epoch: number | null;
  created_at_epoch: number;
  confirmed_at_epoch: number | null;
}

export interface PairingCode {
  pairing_id: string;
  code: string;
  expires_at_epoch: number;
}

export async function createDevicePairingCode(): Promise<PairingCode> {
  return apiRequest<PairingCode>('/api/v1/devices/pairing-codes', { method: 'POST', body: '{}' });
}

export async function listPairedDevices(): Promise<{ items: PairedDevice[] }> {
  return apiRequest<{ items: PairedDevice[] }>('/api/v1/devices');
}

export async function confirmPairedDevice(deviceId: string): Promise<PairedDevice> {
  return apiRequest<PairedDevice>(`/api/v1/devices/${deviceId}/confirm`, { method: 'POST', body: '{}' });
}

export async function savePairedDeviceSettings(
  device: PairedDevice,
  selectedChats: DeviceChat[],
  syncIntervalMinutes = (device.settings.sync_interval_seconds ?? 300) / 60,
  collection?: Pick<DeviceSettings, 'attachments_enabled' | 'attachment_kinds' | 'attachment_auto_notice' | 'chat_selection_mode' | 'excluded_chat_ids'>,
): Promise<PairedDevice> {
  return apiRequest<PairedDevice>(`/api/v1/devices/${device.id}/settings`, {
    method: 'PATCH',
    body: JSON.stringify({
      expected_version: device.version,
      selected_chats: selectedChats,
      attachments_enabled: device.settings.attachments_enabled,
      ...collection,
      sync_interval_seconds: syncIntervalMinutes * 60,
    }),
  });
}

export async function requestPairedDeviceSync(deviceId: string): Promise<{ note: string }> {
  // 与 Agent 共用服务端请求队列，离线时保留请求，收到回执后才说明完成。
  return apiRequest<{ note: string }>(`/api/v1/devices/${deviceId}/sync`, { method: 'POST', body: '{}' });
}

export async function revokePairedDevice(deviceId: string): Promise<PairedDevice> {
  return apiRequest<PairedDevice>(`/api/v1/devices/${deviceId}`, { method: 'DELETE' });
}

export interface CollectedAttachment {
  id: string; device_id: string; parent_source_id: string; attachment_key: string; filename: string;
  kind: 'image' | 'file'; size_bytes: number; status: string; error_code: string | null;
  auto_notice: boolean; source_document_id: string | null; download_href: string | null;
  created_at_epoch: number; updated_at_epoch: number;
}

export function listCollectedAttachments(sourceId?: string): Promise<{ items: CollectedAttachment[] }> {
  return apiRequest(`/api/v1/devices/attachments${sourceId ? `?source=${encodeURIComponent(sourceId)}` : ''}`);
}

export function retryCollectedAttachment(id: string): Promise<{ id: string; status: string }> {
  return apiRequest(`/api/v1/devices/attachments/${id}/retry`, { method: 'POST', body: '{}' });
}
