import { apiRequest } from './client';
import type { ChangeSetSummary } from './business';

export type AgentThinkingLevel = 'off' | 'low' | 'medium' | 'high' | 'max';

// 标签保持产品语义，具体档位由模型能力裁剪，不在前端假设每家供应商都完全相同。
export const AGENT_THINKING_OPTIONS: Array<{ value: AgentThinkingLevel; label: string; hint: string }> = [
  { value: 'medium', label: '标准（推荐）', hint: '先核对日程和约束，再给出完整建议' },
  { value: 'high', label: '深入', hint: '适合复杂规划、冲突取舍和多步安排' },
  { value: 'max', label: '最高', hint: '模型支持时使用最长思考，耗时和费用更高' },
  { value: 'low', label: '快速', hint: '适合简单查询，回复更简短' },
  { value: 'off', label: '关闭', hint: '不启用推理思考，速度优先' },
];

export interface AgentConversationSummary {
  id: string;
  is_main?: boolean;
  title: string;
  status: 'active' | 'archived';
  version: number;
  updated_at_epoch: number;
  archived_at_epoch?: number | null;
  memory?: { summary: string; through_sequence: number } | null;
}

export interface AgentTurn {
  id: string;
  run_id: string | null;
  sequence: number;
  role: 'user' | 'assistant';
  content: string;
  metadata: Record<string, unknown>;
  created_at_epoch: number;
}

export interface AgentRun {
  id: string;
  conversation_id: string;
  status: string;
  result: { assistant_turn?: AgentTurn } | null;
  error_code: string | null;
  attempt: number;
  created_at_epoch: number;
  updated_at_epoch: number;
}

export async function listAgentConversations(status: 'active' | 'archived' = 'active'): Promise<{ items: AgentConversationSummary[] }> {
  return apiRequest(`/api/v1/agent/conversations?status=${status}`);
}

export async function getAgentConversation(id: string, afterSequence?: number): Promise<{
  conversation: AgentConversationSummary;
  turns: AgentTurn[];
  active_run_id?: string | null;
  last_failed_run_id?: string | null;
  latest_sequence: number;
  reset_required: boolean;
  change_sets: Record<string, ChangeSetSummary>;
}> {
  const query = typeof afterSequence === 'number' ? `?after_sequence=${afterSequence}` : '';
  return apiRequest(`/api/v1/agent/conversations/${id}${query}`);
}

export async function submitAgentTurn(payload: {
  conversation_id?: string;
  profile_id?: string;
  thinking_level?: AgentThinkingLevel;
  idempotency_key: string;
  message: string;
  attachment_ids?: string[];
  page_context?: string;
}): Promise<{ conversation_id: string; run_id: string; status: string }> {
  return apiRequest('/api/v1/agent/turns', { method: 'POST', body: JSON.stringify(payload) });
}

export async function getAgentRun(id: string): Promise<AgentRun> {
  return apiRequest(`/api/v1/agent/runs/${id}`);
}

export async function archiveAgentConversation(id: string, expectedVersion: number): Promise<AgentConversationSummary> {
  return apiRequest(`/api/v1/agent/conversations/${id}/archive`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export async function restoreAgentConversation(id: string, expectedVersion: number): Promise<AgentConversationSummary> {
  return apiRequest(`/api/v1/agent/conversations/${id}/restore`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion }),
  });
}

export async function cancelAgentRun(id: string): Promise<{ status: string }> {
  return apiRequest(`/api/v1/agent/runs/${id}/cancel`, { method: 'POST', body: '{}' });
}

export async function retryAgentRun(id: string, options: { profileId?: string; thinkingLevel?: AgentThinkingLevel } = {}): Promise<{ run_id: string; status: string }> {
  return apiRequest(`/api/v1/agent/runs/${id}/retry`, {
    method: 'POST',
    body: JSON.stringify({
      ...(options.profileId ? { profile_id: options.profileId } : {}),
      ...(options.thinkingLevel ? { thinking_level: options.thinkingLevel } : {}),
    }),
  });
}

export type { ChangeSetSummary };

export interface AgentAttachment {
  id: string;
  filename: string;
  status: string;
  image_url: string | null;
  total_characters: number;
  ocr_status?: string | null;
}

export interface AgentPreferences {
  profile_id: string | null;
  automatic_notifications: boolean;
  context_budget: number;
  thinking_level: AgentThinkingLevel;
  main_conversation_id?: string | null;
}

export interface AgentMemory {
  id: string;
  category: 'preference' | 'constraint' | 'routine' | 'goal' | 'other';
  content: string;
  source_conversation_id: string | null;
  created_at_epoch: number;
  updated_at_epoch: number;
}

// 上传与后续轮询复用统一认证和 CSRF，不在浏览器保存附件正文。
export async function uploadAgentAttachment(file: File): Promise<{ attachment: AgentAttachment }> {
  const body = new FormData(); body.append('file', file);
  return apiRequest('/api/v1/agent/attachments', { method: 'POST', body });
}
export async function getAgentAttachment(id: string): Promise<{ attachment: AgentAttachment }> {
  return apiRequest(`/api/v1/agent/attachments/${id}`);
}
export async function getAgentPreferences(): Promise<AgentPreferences> {
  return apiRequest('/api/v1/agent/preferences');
}
export async function saveAgentPreferences(preferences: AgentPreferences): Promise<AgentPreferences> {
  const { main_conversation_id: _main, ...body } = preferences;
  return apiRequest('/api/v1/agent/preferences', { method: 'PUT', body: JSON.stringify(body) });
}
export async function restoreMainConversation(): Promise<{ conversation_id: string }> {
  return apiRequest('/api/v1/agent/main', { method: 'POST', body: '{}' });
}

export async function listAgentMemories(): Promise<{ items: AgentMemory[] }> {
  return apiRequest('/api/v1/agent/memories');
}

export async function addAgentMemory(content: string, category: AgentMemory['category'] = 'other'): Promise<{ memory: AgentMemory }> {
  return apiRequest('/api/v1/agent/memories', {
    method: 'POST',
    body: JSON.stringify({ content, category }),
  });
}

export async function deleteAgentMemory(id: string): Promise<void> {
  await apiRequest(`/api/v1/agent/memories/${id}`, { method: 'DELETE' });
}

export interface AgentStreamEvent { sequence: number; type: string; text?: string; tool_name?: string; success?: boolean; }
export async function streamAgentEvents(id: string, after: number, accept: (event: AgentStreamEvent) => void, signal: AbortSignal): Promise<void> {
  // 直接消费 SSE 的文本增量，重连游标由调用方保存，Cookie 仅用于同源认证。
  const response = await fetch(`/api/v1/agent/runs/${id}/stream?after=${after}`, { signal, credentials: 'same-origin' });
  if (!response.ok || !response.body) throw new Error('无法连接助手事件流。');
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
  try {
    while (true) {
      const result = await reader.read(); if (result.done) break;
      buffer += decoder.decode(result.value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
        const data = frame.split('\n').find(line => line.startsWith('data: '));
        if (data) accept(JSON.parse(data.slice(6)) as AgentStreamEvent);
      }
    }
  } finally { reader.releaseLock(); }
}
