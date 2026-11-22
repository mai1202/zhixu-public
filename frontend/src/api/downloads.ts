import { apiRequest } from './client';

export interface ClientDownloadInfo {
  available: boolean;
  url: string | null;
  filename: string | null;
  size_bytes: number | null;
  sha256: string | null;
  updated_at_epoch: number | null;
  external: boolean;
}

export async function getClientDownload(): Promise<ClientDownloadInfo> {
  return apiRequest<ClientDownloadInfo>('/api/v1/client/download');
}
