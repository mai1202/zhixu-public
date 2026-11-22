import { apiRequest } from './client';

export type KnowledgeKind = 'all' | 'document' | 'notification' | 'memory' | 'conversation' | 'attachment';
export type KnowledgeScope = 'all' | 'private' | 'public';
export type SearchMode = 'auto' | 'local' | 'multi';

export interface KnowledgeMetadata {
  publisher?: string | null;
  published_date?: string | null;
  effective_status?: string | null;
  audience?: string | null;
  supersedes_id?: string | null;
  fetched_at_epoch?: number | null;
}

export interface Citation {
  kind: string;
  id: string | null;
  title: string;
  href: string | null;
  source_url: string | null;
  quote: string;
  updated_at_epoch: number | null;
  version: number | null;
  channel: string;
  publisher?: string | null;
  published_date?: string | null;
  effective_status?: string | null;
  audience?: string | null;
}

export interface KnowledgeItem {
  channel: string;
  channels: string[];
  kind: string;
  id: string;
  title: string;
  snippet: string;
  url: string | null;
  source_url: string | null;
  href: string | null;
  scope: string;
  version: number;
  score: number;
  matched_by?: string[];
  updated_at_epoch?: number | null;
  fetched_at_epoch?: number | null;
  metadata?: KnowledgeMetadata;
  citation: Citation;
}

export interface KnowledgeSearchResult {
  query: string;
  public_query?: string | null;
  mode: string;
  web: string;
  items: KnowledgeItem[];
  total: number;
  has_more: boolean;
  channels: Record<string, { status: string; count: number; code?: string | null; message?: string | null; method?: string }>;
  method: string;
  retrieval?: Record<string, unknown>;
  evidence_note: string;
  fetched_at_epoch: number;
  elapsed_ms: number;
  cached: boolean;
  citations: Citation[];
  warning: string;
}

export interface KnowledgeDetail {
  id: string;
  kind: string;
  title: string;
  scope: string;
  content: string;
  offset: number;
  total_characters: number;
  next_offset: number | null;
  version: number;
  status: string;
  source_url: string | null;
  metadata: KnowledgeMetadata;
}

export interface KnowledgeStatus {
  search: {
    local: { status: string; method: string };
    duckduckgo: { status: string; configured: boolean; installed: boolean; backend: string };
    brave: { status: string; configured: boolean };
    retrieval: { enabled: boolean; configured: boolean; method: string; model: string; dimensions: number; note: string };
  };
  documents: { private: number; public: number };
  mail_ingress: MailIngressStatus;
  note: string;
}

export interface MailIngressStatus {
  recipient: string | null;
  configured: boolean;
  subject_tag: string | null;
  recent: { status: string; updated_at_epoch: number; source_id: string | null; warnings: { filename?: string; code?: string }[] }[];
  note: string;
}

export function searchKnowledge(query: string, options: {
  mode?: SearchMode; scope?: KnowledgeScope; kind?: KnowledgeKind; limit?: number; web?: 'auto' | 'never' | 'always'; refresh?: boolean;
} = {}): Promise<KnowledgeSearchResult> {
  const params = new URLSearchParams({ q: query, mode: options.mode ?? 'auto', scope: options.scope ?? 'all',
    kind: options.kind ?? 'all', limit: String(options.limit ?? 10), web: options.web ?? 'auto' });
  if (options.refresh) params.set('refresh', '1');
  return apiRequest<KnowledgeSearchResult>('/api/v1/knowledge/search?' + params.toString());
}

export function readKnowledgeEntry(kind: string, id: string, offset = 0): Promise<KnowledgeDetail> {
  return apiRequest<KnowledgeDetail>('/api/v1/knowledge/entries/' + encodeURIComponent(kind) + '/' + encodeURIComponent(id) + '?offset=' + offset);
}

export function getKnowledgeStatus(): Promise<KnowledgeStatus> {
  return apiRequest<KnowledgeStatus>('/api/v1/knowledge/status');
}

export function saveKnowledgeDocument(payload: {
  title: string; content: string; scope: 'private' | 'public'; source_url?: string | null;
  publisher?: string | null; published_date?: string | null;
  effective_status?: 'effective' | 'expired' | 'unknown'; audience?: string | null;
}): Promise<{ id: string; version: number }> {
  return apiRequest('/api/v1/knowledge/documents', { method: 'POST', body: JSON.stringify(payload) });
}

export function archiveKnowledgeDocument(id: string, expectedVersion: number): Promise<{ id: string; status: string; version: number }> {
  return apiRequest('/api/v1/knowledge/documents/' + encodeURIComponent(id), {
    method: 'PATCH', body: JSON.stringify({ expected_version: expectedVersion, status: 'archived' }),
  });
}

export function importWebPage(url: string, scope: 'private' | 'public'): Promise<KnowledgeDetail & { links: string[]; warning: string }> {
  return apiRequest('/api/v1/knowledge/web-pages', { method: 'POST', body: JSON.stringify({ url, scope }) });
}

export function getMailIngressStatus(): Promise<MailIngressStatus> {
  return apiRequest('/api/v1/mail-ingress');
}

export function rotateMailIngressToken(): Promise<MailIngressStatus> {
  return apiRequest('/api/v1/mail-ingress/token', { method: 'POST', body: '{}' });
}

export function importEml(file: File): Promise<{ status: string; source_id: string; attachments: { id: string; filename: string }[]; warnings: { filename?: string; code?: string }[] }> {
  const data = new FormData();
  data.append('file', file);
  return apiRequest('/api/v1/mail-ingress/import', { method: 'POST', body: data });
}
