import { apiRequest } from './client';

export type InsightModule = 'course' | 'task' | 'notice';

export interface AgentInsightItem {
  id: string;
  module: InsightModule;
  kind: string;
  severity: 'critical' | 'warning' | 'info';
  title: string;
  detail: string;
  object_type: string | null;
  object_id: string | null;
  action_href: string | null;
  metadata: Record<string, unknown>;
  status: string;
  version: number;
  created_at_epoch: number;
  updated_at_epoch: number;
}

export interface InsightList {
  items: AgentInsightItem[];
  counts: { critical: number; warning: number; info: number };
}

export function listInsights(module?: InsightModule): Promise<InsightList> {
  const query = module ? `?module=${module}` : '';
  return apiRequest(`/api/v1/insights${query}`);
}

export function scanInsights(): Promise<InsightList & { active: number }> {
  return apiRequest('/api/v1/insights/scan', { method: 'POST', body: '{}' });
}

export function dismissInsight(item: AgentInsightItem): Promise<AgentInsightItem> {
  return apiRequest(`/api/v1/insights/${item.id}/dismiss`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: item.version }),
  });
}
