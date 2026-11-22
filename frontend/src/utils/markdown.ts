import DOMPurify from 'dompurify';
import { marked } from 'marked';

let hookInstalled = false;
function installLinkHook(): void {
  if (hookInstalled) return;
  hookInstalled = true;
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      const href = node.getAttribute('href') ?? '';
      if (/^https?:\/\//i.test(href)) {
        node.setAttribute('target', '_blank');
        node.setAttribute('rel', 'noopener noreferrer');
      }
    }
  });
}

/** 代码围栏外合并多余空行，保留代码块内部原样。 */
export function normalizeMarkdownSource(source: string): string {
  const value = String(source ?? '').replace(/\r\n?/g, '\n').replace(/\u200b/g, '');
  return value
    .split(/(```[\s\S]*?```|~~~[\s\S]*?~~~)/g)
    .map((part, index) => (index % 2 === 1 ? part : part.replace(/\n{3,}/g, '\n\n')))
    .join('')
    .replace(/^\n+/, '')
    .replace(/\n+$/, '');
}

/** 流式期间补齐未闭合围栏，避免 marked 把后续内容吞进代码块。 */
export function closeOpenFence(source: string): string {
  let fence = '';
  for (const line of source.split('\n')) {
    const match = line.match(/^\s*(```+|~~~+)/);
    if (!match) continue;
    const marker = match[1][0].repeat(3);
    if (!fence) fence = marker;
    else if (marker === fence) fence = '';
  }
  return fence ? `${source}\n${fence}` : source;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const cache = new Map<string, string>();
const CACHE_LIMIT = 200;

export function renderMarkdown(source: string, streaming = false): string {
  const normalized = normalizeMarkdownSource(source);
  if (!normalized.trim()) return '';
  if (!streaming) {
    const cached = cache.get(normalized);
    if (cached !== undefined) return cached;
  }
  installLinkHook();
  const input = streaming ? closeOpenFence(normalized) : normalized;
  let html = '';
  try {
    html = DOMPurify.sanitize(marked.parse(input, { gfm: true, breaks: false }) as string);
  } catch {
    html = `<p>${escapeHtml(input)}</p>`;
  }
  if (!streaming) {
    cache.set(normalized, html);
    if (cache.size > CACHE_LIMIT) {
      const oldest = cache.keys().next().value;
      if (oldest !== undefined) cache.delete(oldest);
    }
  }
  return html;
}

export function clearMarkdownCache(): void {
  cache.clear();
}
