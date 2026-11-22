const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * 会话和自动处理卡片共用的人性化时间：
 * 当天 x:xx、昨天 x:xx、同年 m/dd 周x x:xx、跨年 yyyy/m/dd 周x x:xx。
 */
export function formatRelativeDateTime(epochSeconds: number, now = new Date()): string {
  const date = new Date(epochSeconds * 1000);
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const dayDelta = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  if (dayDelta === 0) return clock;
  if (dayDelta === 1) return `昨天 ${clock}`;
  const datePart = `${date.getMonth() + 1}/${pad(date.getDate())} ${WEEKDAYS[date.getDay()]}`;
  if (date.getFullYear() === now.getFullYear()) return `${datePart} ${clock}`;
  return `${date.getFullYear()}/${datePart} ${clock}`;
}

export function formatConversationDate(epochSeconds: number, now = new Date()): string {
  const date = new Date(epochSeconds * 1000);
  const dayDelta = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  if (dayDelta === 0) return '今天';
  if (dayDelta === 1) return '昨天';
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}/${pad(date.getDate())}`;
  return `${date.getFullYear()}/${date.getMonth() + 1}/${pad(date.getDate())}`;
}
