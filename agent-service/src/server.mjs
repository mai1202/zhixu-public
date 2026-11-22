import { createServer } from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { resolve } from 'node:path';
import { runPi, searchPiHistory, hasPersistedSession } from './runtime.mjs';
import { classifyProviderFailure } from './provider-failure.mjs';

const token = process.env.PI_BRIDGE_TOKEN;
if (!token || token.length < 32) throw new Error('PI_BRIDGE_TOKEN must be configured');
const root = resolve(process.env.DATA_DIR ?? 'data', 'private', 'pi');
const callbackUrl = process.env.PI_CALLBACK_URL ?? 'http://127.0.0.1:5000';
const active = new Map();
const queues = new Map();

async function body(request) {
  // 请求仅来自本机 Flask，仍限制大小以防误配置或超大附件。
  let size = 0; const chunks = [];
  for await (const chunk of request) { size += chunk.length; if (size > 30 * 1024 * 1024) throw new Error('request_too_large'); chunks.push(chunk); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
function authorized(request) {
  const supplied = Buffer.from((request.headers.authorization ?? '').replace(/^Bearer /, ''));
  const expected = Buffer.from(token);
  return supplied.length === expected.length && timingSafeEqual(supplied, expected);
}
const server = createServer(async (request, response) => {
  response.setHeader('Cache-Control', 'no-store');
  if (!authorized(request)) { response.writeHead(401).end(); return; }
  try {
    if (request.method === 'GET' && request.url === '/health') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify({ status: 'ok', sdk: '1.3.0', active: active.size })); return; }
    const input = await body(request);
    if (request.url === '/session-info') { response.end(JSON.stringify({ initialized: hasPersistedSession(root, input.user_id, input.conversation_id) })); return; }
    if (request.url === '/shutdown') { response.end(JSON.stringify({ stopping: true })); setImmediate(shutdown); return; }
    if (request.url === '/cancel') { const session = active.get(input.run_id); if (session) await session.abort(); response.end(JSON.stringify({ cancelled: Boolean(session) })); return; }
    if (request.url === '/history') { response.setHeader('Content-Type', 'application/json'); response.end(JSON.stringify(await searchPiHistory(root, input.user_id, input.conversation_id, input.query, input.offset))); return; }
    if (request.url !== '/runs' || request.method !== 'POST') { response.writeHead(404).end(); return; }
    response.writeHead(200, { 'Content-Type': 'application/x-ndjson; charset=utf-8' });
    const key = `${input.user_id}/${input.conversation_id}`;
    // 每段会话串行排队；其他账户可并行，不把后台事件插进一轮未完成的工具调用。
    const previous = queues.get(key) ?? Promise.resolve();
    const operation = previous.catch(() => {}).then(() => runPi(input, { root, callbackUrl,
      emit: event => response.write(JSON.stringify(event) + '\n'), onSession: session => active.set(input.run_id, session),
    }));
    queues.set(key, operation);
    try { await operation; }
    catch (error) {
      // 只映射稳定错误类别，供应商响应正文、提示词和密钥均不进入网页或日志。
      const classified = classifyProviderFailure(error);
      const code = classified === 'provider_request_failed' ? 'model_request_failed' : classified;
      const messages = {
        platform_token_quota_exceeded: '今日平台模型预算已用完（每日按人民币计价），请明天再试或绑定自己的模型。',
        platform_budget_unavailable: '平台额度服务暂不可用，本轮未继续请求模型，请稍后重试。',
        provider_auth_failed: '模型服务拒绝了当前 API Key，请在设置 → 模型连接里检查或轮换密钥。',
        provider_insufficient_balance: '模型服务提示余额或配额不足，请给服务商充值或换一个可用模型。',
        provider_rate_limited: '模型服务正在限流，请稍后重试或切换其他模型配置。',
        provider_timeout: '模型请求超时，请检查网络后重试。',
        provider_unavailable: '暂时连不上模型服务，请检查网络、模型地址和模型 ID。',
        model_unavailable: '模型配置在 Pi 中不可用，请在设置里重新测试连接。',
        model_request_failed: '模型请求失败，输入和已完成步骤已保存，可直接重试。',
        request_too_large: '本轮附件或上下文超过内部上限，请减少附件后重试。',
        pi_run_failed: '助手运行失败，原始会话和未完成工作已保留。',
      };
      response.write(JSON.stringify({ type: 'error', code, message: messages[code] ?? messages.pi_run_failed }) + '\n');
    }
    finally { active.delete(input.run_id); if (queues.get(key) === operation) queues.delete(key); response.end(); }
  } catch { if (!response.headersSent) response.writeHead(400); response.end(JSON.stringify({ error: { code: 'invalid_request' } })); }
});
server.requestTimeout = 0;
server.listen(Number(process.env.PI_AGENT_PORT ?? 5011), '127.0.0.1', () => console.log('Pi service ready on loopback; SDK 1.3.0'));
async function shutdown() { server.close(); await Promise.allSettled([...active.values()].map(session => session.abort())); process.exit(0); }
process.on('SIGTERM', shutdown); process.on('SIGINT', shutdown);
