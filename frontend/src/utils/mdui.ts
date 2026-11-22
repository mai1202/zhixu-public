/** MDUI 自定义元素把最新值同步到宿主元素，统一在这里读取，避免各页重复断言。 */
export function readMduiValue(event: Event): string {
  return String((event.currentTarget as HTMLElement & { value?: string }).value ?? '');
}

export function readMduiChecked(event: Event): boolean {
  return Boolean((event.currentTarget as HTMLElement & { checked?: boolean }).checked);
}

export function readMduiValues(event: Event): string[] {
  const value = (event.currentTarget as HTMLElement & { value?: string | string[] }).value;
  if (Array.isArray(value)) return value.map(String);
  return value ? [String(value)] : [];
}

export function readMduiNumber(event: Event): number {
  const value = Number(readMduiValue(event));
  return Number.isFinite(value) ? value : 0;
}
