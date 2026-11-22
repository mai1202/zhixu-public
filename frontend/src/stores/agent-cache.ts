import type { AgentConversationSummary, AgentTurn } from '../api/agent';

export interface CachedAgentConversation {
  conversation: AgentConversationSummary;
  turns: AgentTurn[];
  latestSequence: number;
  activeRunId: string | null;
  lastFailedRunId: string | null;
  syncedAt: number;
}

const CACHE_LIMIT = 8;
const cache = new Map<string, CachedAgentConversation>();

function key(userId: string, conversationId: string): string {
  return `${userId}:${conversationId}`;
}

function trim(): void {
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

export function getCachedConversation(userId: string, conversationId: string): CachedAgentConversation | null {
  const value = cache.get(key(userId, conversationId));
  if (!value) return null;
  // 重新插入以维持 LRU 顺序。
  cache.delete(key(userId, conversationId));
  cache.set(key(userId, conversationId), value);
  return value;
}

export function putCachedConversation(userId: string, value: CachedAgentConversation): void {
  cache.set(key(userId, value.conversation.id), value);
  trim();
}

export function deleteCachedConversation(userId: string, conversationId: string): void {
  cache.delete(key(userId, conversationId));
}

export function clearConversationCache(userId?: string): void {
  if (!userId) {
    cache.clear();
    return;
  }
  for (const item of [...cache.keys()]) {
    if (item.startsWith(`${userId}:`)) cache.delete(item);
  }
}

export function mergeTurns(current: AgentTurn[], incoming: AgentTurn[]): AgentTurn[] {
  const byId = new Map(current.map((turn) => [turn.id, turn]));
  for (const turn of incoming) byId.set(turn.id, turn);
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence);
}

export function latestSequence(turns: AgentTurn[]): number {
  return turns.reduce((latest, turn) => Math.max(latest, turn.sequence), 0);
}
