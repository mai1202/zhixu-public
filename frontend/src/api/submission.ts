// 仅持久化请求摘要和随机提交标识；不把通知正文、模型密钥或邮箱写入浏览器存储。
const memoryKeys = new Map<string, string>();

export async function sourceSubmission(owner: string, payload: unknown): Promise<{ key: string; complete: () => void }> {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(payload)));
  const digest = Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, '0')).join('');
  const storageKey = `nm-source-submission:${owner}:${digest}`;
  let key = memoryKeys.get(storageKey);
  try { key ??= sessionStorage.getItem(storageKey) ?? undefined; } catch { /* 禁用存储时仍保障本页重试。 */ }
  key ??= crypto.randomUUID();
  memoryKeys.set(storageKey, key);
  try { sessionStorage.setItem(storageKey, key); } catch { /* 不因浏览器存储权限阻断保存。 */ }
  return { key, complete: () => {
    // 收到明确的服务端回执后结束此次提交，允许用户再次独立录入相同文字。
    memoryKeys.delete(storageKey);
    try { sessionStorage.removeItem(storageKey); } catch { /* 内存回执已处理。 */ }
  } };
}
