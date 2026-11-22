import type { PeriodGroup } from '../api/timetable';

export function courseTimesForPeriods(periods: number[], profile: PeriodGroup[]): { start: string; end: string } | null {
  // 只有完整覆盖所选小节的作息组才能推导时间，不能平均拆分或延用上次时间。
  const selected = new Set(periods);
  const groups = profile.filter(group => group.periods.some(period => selected.has(period)));
  if (!groups.length || groups.some(group => !group.start_time || !group.end_time || group.periods.some(period => !selected.has(period)))) return null;
  const covered = new Set(groups.flatMap(group => group.periods));
  if (periods.some(period => !covered.has(period))) return null;
  return { start: groups.map(group => group.start_time!).sort()[0], end: groups.map(group => group.end_time!).sort().at(-1)! };
}
