import type { OcrItem, OcrResult } from '@notification-management/contracts';
import { apiRequest } from './client';

export interface OcrArtifact {
  id: string;
  source_document_id: string | null;
  status: 'uploaded' | 'queued' | 'leased' | 'running' | 'retry_wait' | 'succeeded' | 'failed' | 'cancelled' | 'linking';
  failure_code: string | null;
  media_type: string;
  size_bytes: number;
  image_hash: string;
  width: number;
  height: number;
  orientation: number;
  version: number;
  result: OcrResult | null;
  corrected_items: Array<{ id: string; original_text: string; text: string; segment_id: string }> | null;
  image_url: string;
  created_at_epoch: number;
}

export function submitBrowserOcr(image: File, result: OcrResult): Promise<{ artifact: OcrArtifact }> {
  const body = new FormData();
  body.set('image', image, 'screenshot');
  body.set('result', JSON.stringify(result));
  return apiRequest('/api/v1/ocr/browser-results', { method: 'POST', body });
}

export function submitServerOcr(image: File): Promise<{ artifact: OcrArtifact }> {
  const body = new FormData();
  body.set('image', image, 'screenshot');
  return apiRequest('/api/v1/ocr/server-jobs', { method: 'POST', body });
}

export function getOcrArtifact(id: string): Promise<OcrArtifact> {
  return apiRequest(`/api/v1/ocr/artifacts/${id}`);
}

export function createOcrSource(
  artifact: OcrArtifact,
  title: string,
  correctedItems: OcrItem[],
  analysis: { mode: 'rules' | 'llm' | 'auto'; providerProfileId?: string },
): Promise<{ source: { id: string; source_type: string }; change_set: { id: string }; analysis_job_id?: string }> {
  return apiRequest(`/api/v1/ocr/artifacts/${artifact.id}/source`, {
    method: 'POST',
    body: JSON.stringify({
      expected_version: artifact.version,
      title: title || undefined,
      corrected_items: correctedItems,
      analysis_mode: analysis.mode,
      ...(analysis.providerProfileId ? { provider_profile_id: analysis.providerProfileId } : {}),
    }),
  });
}
