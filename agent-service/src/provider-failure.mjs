/**
 * 把提供方异常归入稳定类别，只暴露「密钥 / 余额 / 限流 / 超时 / 网络」这类可执行信息。
 * 首页据此提示用户检查 API Key 或充值；响应正文、提示词和密钥都不外传。
 */

// 已由内部逻辑判定过的错误码不需要再按 HTTP 状态猜测。
const KNOWN_CODES = new Set([
  'platform_token_quota_exceeded', 'platform_budget_unavailable', 'model_unavailable',
  'provider_request_failed', 'request_too_large', 'required_tool_missing',
]);

export function classifyProviderFailure(error) {
  if (error?.code && KNOWN_CODES.has(error.code)) return error.code;
  const status = Number(error?.status ?? error?.statusCode ?? error?.response?.status ?? 0);
  const text = `${error?.code ?? ''} ${error?.message ?? ''}`.toLowerCase();
  if (status === 401 || status === 403
    || /unauthorized|authentication|permission denied/.test(text)
    || /(invalid|incorrect|missing|expired|revoked)[ _-]*(api[ _-]?key|key)/.test(text)
    || /api[ _-]?key[^.]{0,24}(invalid|incorrect|missing|expired|revoked)/.test(text)) return 'provider_auth_failed';
  if (status === 402 || /insufficient|balance|arrears|payment required|quota|credit/.test(text)) return 'provider_insufficient_balance';
  if (status === 429 || /rate[ _-]?limit|too many requests/.test(text)) return 'provider_rate_limited';
  if (status === 408 || status === 504 || /timeout|timed out|etimedout|request was aborted/.test(text)) return 'provider_timeout';
  if (status >= 500 || /econnrefused|enotfound|eai_again|network|fetch failed|socket hang up/.test(text)) return 'provider_unavailable';
  return 'model_request_failed';
}
