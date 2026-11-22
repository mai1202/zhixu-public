// 由后端 Pydantic 契约生成；修改结构请先改服务端模型。

export interface ChangeCandidate {
  temp_id: string;
  target_event_temp_id: string;
  summary: string;
  patch: Record<string, unknown>;
  evidence: Array<EvidenceRef>;
}

export interface DeadlineCandidate {
  raw: string;
  local_date: string | null;
  local_time: string | null;
  timezone: string | null;
  precision: "unknown" | "year" | "month" | "day" | "minute" | "second";
  utc: string | null;
  basis: string | null;
}

export interface EventCandidate {
  temp_id: string;
  notice_temp_id: string | null;
  title: string;
  actor: string | null;
  stage: string | null;
  scope: string | null;
  deadline: DeadlineCandidate | null;
  time_range: TimeRangeCandidate | null;
  materials: Array<string>;
  submission: string | null;
  eligibility: "eligible" | "ineligible" | "unknown" | "conditional";
  evidence: Record<string, Array<EvidenceRef>>;
  unknowns: Array<string>;
}

export interface EvidenceRef {
  segment_id: string;
  quote: string;
}

export interface NoticeCandidate {
  temp_id: string;
  title: string;
  source_ids: Array<string>;
}

export interface ScheduleEntryCandidate {
  temp_id: string;
  title: string;
  kind: "busy_block" | "all_day" | "deadline_marker";
  date: string | null;
  start: string | null;
  end: string | null;
  all_day: boolean;
  source_event_temp_id: string | null;
}

export interface TimeRangeCandidate {
  start: string;
  end: string;
  timezone: string;
}

export interface NormalizedBundle {
  schema_version: "1.0";
  notices: Array<NoticeCandidate>;
  events: Array<EventCandidate>;
  schedule_entries: Array<ScheduleEntryCandidate>;
  change_candidates: Array<ChangeCandidate>;
  warnings: Array<string>;
}
