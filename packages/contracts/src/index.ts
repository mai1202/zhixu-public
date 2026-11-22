// 前后端共享的运行状态类型；业务数据结构由同目录 JSON Schema 维护。
export type {
  ChangeCandidate,
  DeadlineCandidate,
  EvidenceRef,
  EventCandidate,
  NormalizedBundle,
  NoticeCandidate,
  ScheduleEntryCandidate,
  TimeRangeCandidate,
} from './normalized-bundle';
export type { OcrItem, OcrResult } from './ocr-result';

export interface HealthResponse {
  status: 'ok';
  service: 'notification-management-api';
  contract_version: '1.0';
}

export type JobStatus =
  | 'queued'
  | 'leased'
  | 'running'
  | 'succeeded'
  | 'retry_wait'
  | 'failed'
  | 'cancelled'
  | 'expired';

export type JobKind =
  | 'source.normalize'
  | 'ocr.server'
  | 'agent.run'
  | 'agent.notification'
  | 'digest.compose'
  | 'mail.send'
  | 'local_inference';
