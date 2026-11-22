// 直接包装已安装SDK的单次请求流，覆盖聊天、工具续轮、重试与压缩，避免按整段历史重复扣费。
import { randomUUID } from 'node:crypto';
import { streamSimple as openAIStream } from '@earendil-works/pi-ai/api/openai-completions';
import { AssistantMessageEventStream } from '@earendil-works/pi-ai/utils/event-stream';

export function budgetedStream(request, callbackUrl, onBudgetError) {
  async function callback(action, body) {
    const response = await fetch(`${callbackUrl}/api/v1/internal/pi/token-budget/${action}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${request.capability}`, 'X-Pi-Run-Id': request.run_id },
      body: JSON.stringify(body), signal: AbortSignal.timeout(15000),
    });
    const value = await response.json();
    if (!response.ok) {
      const error = Object.assign(new Error(value.error?.message ?? '平台模型额度核验失败，请稍后重试。'), { code: value.error?.code ?? 'platform_budget_unavailable' });
      onBudgetError(error); // SDK会把onPayload异常转成流错误，提前保留明确的额度错误码。
      throw error;
    }
    return value;
  }
  return (model, context, options = {}) => {
    const output = new AssistantMessageEventStream();
    void (async () => {
      let reservation = null;
      let usage = null;
      let settled = false;
      try {
        const inner = openAIStream(model, context, { ...options, maxRetries: 0,
          onPayload: async (payload, actualModel) => {
            const body = await options.onPayload?.(payload, actualModel) ?? payload;
            // 对最终请求显式封顶，思考输出也在max_tokens范围内；SDK内部HTTP重试关闭，外层每次重试重新预留。
            const field = body.max_completion_tokens != null ? 'max_completion_tokens' : 'max_tokens';
            body[field] = Math.min(body[field] ?? request.profile.max_output_tokens, request.profile.max_output_tokens);
            // 视觉 token 与压缩图片字节数无固定比例，带图按模型完整上下文上界预留。
            const hasImage = body.messages?.some(message => Array.isArray(message.content)
              && message.content.some(part => part.type === 'image_url'));
            const hold = await callback('reserve', { reservation_id: randomUUID(), profile_id: request.profile_id,
              prompt_bytes: Math.max(Buffer.byteLength(JSON.stringify(body), 'utf8'), hasImage ? 1000000 : 0), max_output_tokens: body[field] });
            reservation = hold.reservation_id;
            return body;
          },
          onProviderStreamEvent: async (chunk, actualModel) => {
            // 使用提供方原始usage，缓存输入和思考输出已包含在总量中，不能重复叠加。
            if (chunk.usage) usage = chunk.usage;
            else if (chunk.choices?.[0]?.usage) usage = chunk.choices[0].usage;
            await options.onProviderStreamEvent?.(chunk, actualModel);
          },
        });
        for await (const event of inner) {
          if (event.type === 'done' || event.type === 'error') {
            if (reservation) await callback('settle', { reservation_id: reservation, usage });
            settled = true;
          }
          output.push(event);
        }
        output.end(await inner.result());
      } catch (error) {
        onBudgetError(error);
        const message = { role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: 'error', errorMessage: error.message, timestamp: Date.now() };
        output.push({ type: 'error', reason: 'error', error: message });
      } finally {
        // 无回执、取消或异常不能假设免费；结算失败保留数据库预留，服务重启也不能绕过额度。
        if (reservation && !settled) {
          try { await callback('settle', { reservation_id: reservation, usage }); }
          catch (error) { onBudgetError(error); }
        }
      }
    })();
    return output;
  };
}
