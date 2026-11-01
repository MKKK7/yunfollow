import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const MAX_HTML_BYTES = 2 * 1024 * 1024;
const ALLOWED_FETCH_HOSTS = new Set(['t.me', 'telegra.ph']);
const SHARE_HOSTS = new Set(['115.com', 'www.115.com', '115cdn.com', 'www.115cdn.com']);
export const hash = (value) => createHash('sha256').update(value).digest('hex');

export function decodeEntities(value) {
  const named = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
  return value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (all, entity) => {
    if (entity[0] !== '#') return named[entity.toLowerCase()] ?? all;
    const number = entity[1].toLowerCase() === 'x' ? parseInt(entity.slice(2), 16) : Number(entity.slice(1));
    return number > 0 && number <= 0x10ffff && !(number >= 0xd800 && number <= 0xdfff)
      ? String.fromCodePoint(number) : '\ufffd';
  });
}

// Small bounded HTML tree: only data/attributes are read, no page scripts execute.
function tree(html) {
  if (Buffer.byteLength(html) > MAX_HTML_BYTES) throw new Error('HTML exceeds 2 MiB');
  const root = { tag: 'root', attrs: {}, children: [] };
  const stack = [root];
  const source = html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
  const tokens = source.match(/<!--[\s\S]*?-->|<[^>]*>|[^<]+/g) ?? [];
  if (tokens.length > 100000) throw new Error('HTML token limit exceeded');
  for (const token of tokens) {
    if (token.startsWith('<!--') || token.startsWith('<!')) continue;
    if (!token.startsWith('<')) { stack.at(-1).children.push(decodeEntities(token)); continue; }
    const close = /^<\/\s*([\w:-]+)/.exec(token);
    if (close) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tag === close[1].toLowerCase()) { stack.length = i; break; }
      }
      continue;
    }
    const open = /^<\s*([\w:-]+)/.exec(token);
    if (!open) continue;
    const attrs = {};
    const attrSource = token.slice(open[0].length).replace(/\/?\s*>$/, '');
    for (const m of attrSource.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      attrs[m[1].toLowerCase()] = decodeEntities(m[2] ?? m[3] ?? m[4] ?? '');
    }
    const node = { tag: open[1].toLowerCase(), attrs, children: [] };
    stack.at(-1).children.push(node);
    if (!/\/\s*>$/.test(token) && !['img', 'br', 'meta', 'link', 'input', 'hr', 'source', 'wbr'].includes(node.tag)) {
      if (stack.length >= 100) throw new Error('HTML nesting limit exceeded');
      stack.push(node);
    }
  }
  return root;
}
function nodes(root, predicate) {
  const found = [];
  function visit(node) {
    if (typeof node === 'string') return;
    if (predicate(node)) found.push(node);
    node.children.forEach(visit);
  }
  visit(root); return found;
}
const hasClass = (node, name) => (node.attrs.class ?? '').split(/\s+/).includes(name);
function text(node) {
  if (typeof node === 'string') return node;
  if (node.tag === 'br') return '\n';
  return node.children.map(text).join('') + (['p', 'blockquote'].includes(node.tag) ? '\n' : '');
}
function cleanText(node) { return text(node).replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n').trim(); }
export function safeFetchUrl(input) {
  const url = new URL(input);
  if (url.protocol !== 'https:' || !ALLOWED_FETCH_HOSTS.has(url.hostname) || url.username || url.password || (url.port && url.port !== '443')) {
    throw new Error('Fetch URL must use an allowlisted HTTPS host');
  }
  return url;
}
function telegraphUrl(input) {
  try {
    const u = safeFetchUrl(input);
    if (u.hostname !== 'telegra.ph' || u.pathname === '/') return null;
    u.search = ''; u.hash = ''; return u.href;
  } catch { return null; }
}
export function parseChannel(html, channel = 'Lsp115') {
  if (!/^[A-Za-z0-9_]{5,32}$/.test(channel)) throw new Error('Invalid channel username');
  const root = tree(html);
  const messages = nodes(root, (n) => hasClass(n, 'tgme_widget_message') && n.attrs['data-post']);
  return messages.flatMap((message) => {
    const post = message.attrs['data-post'];
    const parts = /^([A-Za-z0-9_]+)\/(\d+)$/.exec(post);
    if (!parts || parts[1].toLowerCase() !== channel.toLowerCase()) return [];
    const body = nodes(message, (n) => hasClass(n, 'tgme_widget_message_text'))[0];
    const bodyText = body ? cleanText(body) : '';
    const timestamp = nodes(message, (n) => n.tag === 'time')[0]?.attrs.datetime ?? null;
    const buttons = nodes(message, (n) => n.tag === 'a' && hasClass(n, 'tgme_widget_message_inline_button'));
    const resourceButtons = buttons.filter((b) => /查看资源|资源链接|下载链接/.test(cleanText(b)));
    const anchors = [...(body ? nodes(body, (n) => n.tag === 'a') : []), ...resourceButtons];
    const articleUrls = [...new Set(anchors.map((n) => telegraphUrl(n.attrs.href)).filter(Boolean))].slice(0, 3);
    return [{ source: 'telegram', channel: parts[1], messageId: parts[2], sourceUrl: `https://t.me/${post}`,
      timestamp, title: bodyText.split('\n')[0].replace(/^[\p{Extended_Pictographic}\uFE0F\s]+/u, '').trim(), text: bodyText, articleUrls,
      messageHash: hash(JSON.stringify({ bodyText, timestamp, articleUrls })) }];
  }).sort((a, b) => Number(a.messageId) - Number(b.messageId));
}

export function parseArticle(html, articleUrl) {
  const root = tree(html);
  const article = nodes(root, (n) => n.tag === 'article')[0];
  if (!article) throw new Error('No Telegraph article found');
  const articleText = cleanText(article);
  const tmdbMatch = /TMDB\s*ID\s*[:：]?\s*(\d+)/i.exec(articleText);
  const shares = [];
  const offlineLinks = [];
  const seen = new Set();
  for (const a of nodes(article, (n) => n.tag === 'a' && n.attrs.href)) {
    try {
      const u = new URL(a.attrs.href);
      if (u.protocol === 'magnet:' || u.protocol === 'ed2k:') { if (a.attrs.href.length <= 4096) offlineLinks.push(a.attrs.href); continue; }
      if (u.protocol !== 'https:' || !SHARE_HOSTS.has(u.hostname) || u.username || u.password || (u.port && u.port !== '443')) continue;
      const code = /^\/s\/([A-Za-z0-9]+)\/?$/.exec(u.pathname)?.[1];
      if (!code || seen.has(code)) continue;
      const password = u.searchParams.get('password') ?? u.searchParams.get('receive_code') ??
        /(?:提取码|访问码)\s*[:：]?\s*([A-Za-z0-9]{4,8})/.exec(articleText)?.[1] ?? '';
      if (password && !/^[A-Za-z0-9]{1,16}$/.test(password)) continue;
      const canonical = new URL(`https://115cdn.com/s/${code}`);
      if (password) canonical.searchParams.set('password', password);
      seen.add(code); shares.push({ provider: '115', shareCode: code, password, url: canonical.href });
    } catch { /* Ignore non-URL and unrelated links. */ }
  }
  return { articleUrl, title: nodes(article, (n) => n.tag === 'h1')[0] ? cleanText(nodes(article, (n) => n.tag === 'h1')[0]) : null,
    tmdbId: tmdbMatch?.[1] ?? null, evidence: articleText.slice(0, 6000), shares, offlineLinks: offlineLinks.slice(0, 20), articleHash: hash(JSON.stringify({ articleText, shares })) };
}

export async function fetchHtml(input, { fetchImpl = fetch, timeoutMs = 15000, maxBytes = MAX_HTML_BYTES } = {}) {
  let url = safeFetchUrl(input);
  for (let redirects = 0; redirects <= 3; redirects++) {
    const response = await fetchImpl(url.href, { redirect: 'manual', signal: AbortSignal.timeout(timeoutMs),
      headers: { 'User-Agent': 'FNOS-ReadOnly-SourceAdapter/1.0', Accept: 'text/html' } });
    if (response.status >= 300 && response.status < 400) {
      await response.body?.cancel();
      if (redirects === 3) throw new Error('Redirect limit exceeded');
      const location = response.headers.get('location');
      if (!location) throw new Error('Redirect has no Location');
      url = safeFetchUrl(new URL(location, url).href); continue;
    }
    if (!response.ok) { await response.body?.cancel(); throw new Error(`HTTP ${response.status}`); }
    if (!/text\/html|application\/xhtml\+xml/i.test(response.headers.get('content-type') ?? '')) {
      await response.body?.cancel(); throw new Error('Expected HTML, refusing media/non-HTML response');
    }
    if (Number(response.headers.get('content-length') ?? 0) > maxBytes) {
      await response.body?.cancel(); throw new Error('HTML Content-Length limit exceeded');
    }
    const chunks = []; let size = 0;
    try {
      for await (const chunk of response.body) {
        size += chunk.length;
        if (size > maxBytes) throw new Error('HTML streaming size limit exceeded');
        chunks.push(Buffer.from(chunk));
      }
    } catch (error) { await response.body?.cancel().catch(() => {}); throw error; }
    return Buffer.concat(chunks).toString('utf8');
  }
  throw new Error('Unreachable redirect state');
}

export function applyObservation(state, message, articles) {
  const key = `${message.channel.toLowerCase()}/${message.messageId}`;
  const contentHash = hash(JSON.stringify({ message: message.messageHash, articles: articles.map((a) => [a.articleUrl, a.articleHash]) }));
  const previous = state.messages[key];
  if (previous?.contentHash === contentHash) return null;
  state.messages[key] = { contentHash, messageHash: message.messageHash,
    articleHashes: Object.fromEntries(articles.map((a) => [a.articleUrl, a.articleHash])), observedAt: new Date().toISOString() };
  return { event: previous ? 'edited' : 'new', eventId: hash(`${key}:${contentHash}`), ...message, articles };
}

export async function collect({ channel = 'Lsp115', search = '', limit = 5, state = { version: 1, messages: {} }, getHtml = fetchHtml, onSnapshot = async () => {} } = {}) {
  if (!Number.isInteger(limit) || limit < 1 || limit > 20) throw new Error('limit must be 1..20');
  if (typeof search !== 'string' || search.length > 100 || /[\x00-\x1f]/.test(search)) throw new Error('Invalid search query');
  const url = `https://t.me/s/${channel}` + (search ? `?q=${encodeURIComponent(search)}` : '');
  safeFetchUrl(url);
  if (!/^[A-Za-z0-9_]{5,32}$/.test(channel)) throw new Error('Invalid channel username');
  const html = await getHtml(url); await onSnapshot('channel.html', html);
  const messages = parseChannel(html, channel).slice(-limit);
  if (!messages.length && !search) throw new Error('No channel messages: possible landing page or access restriction');
  if (!messages.length && search && !/class=["'][^"']*tgme_channel_info/.test(html)) throw new Error('Public channel search unavailable');
  const events = []; const observations = []; const errors = []; const articleCache = new Map();
  for (const message of messages) {
    try {
      const articles = [];
      for (const articleUrl of message.articleUrls) {
        if (!articleCache.has(articleUrl)) {
          const page = await getHtml(articleUrl);
          await onSnapshot(`article-${hash(articleUrl).slice(0, 16)}.html`, page);
          articleCache.set(articleUrl, parseArticle(page, articleUrl));
        }
        articles.push(articleCache.get(articleUrl));
      }
      observations.push({ ...message, articles });
      const event = applyObservation(state, message, articles);
      if (event) events.push(event);
    } catch (error) {
      // Preserve the previous successful record; do not mark failed observations processed.
      errors.push({ sourceUrl: message.sourceUrl, error: error.message });
    }
  }
  return { observedAt: new Date().toISOString(), channel, polledMessages: messages.length, observations, events, errors, state };
}

async function atomicJson(filename, data) {
  const tmp = `${filename}.tmp`;
  await writeFile(tmp, JSON.stringify(data, null, 2) + '\n', { mode: 0o600 });
  await rename(tmp, filename);
}
async function main(argv) {
  const options = { channel: 'Lsp115', limit: 5, snapshotDir: './telegram-snapshots' };
  const names = { '--channel': 'channel', '--search': 'search', '--limit': 'limit', '--snapshot-dir': 'snapshotDir' };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--help') { console.log('node telegram.mjs [--channel Lsp115] [--limit 1..20] [--snapshot-dir DIR]'); return; }
    const key = names[argv[i]];
    if (!key || !argv[i + 1] || argv[i + 1].startsWith('--')) throw new Error('Unknown or missing CLI option');
    options[key] = key === 'limit' ? Number(argv[++i]) : argv[++i];
  }
  const dir = path.resolve(options.snapshotDir); await mkdir(dir, { recursive: true, mode: 0o700 });
  let state = { version: 1, messages: {} };
  try { state = JSON.parse(await readFile(path.join(dir, 'state.json'), 'utf8')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (state.version !== 1 || !state.messages || typeof state.messages !== 'object' || Array.isArray(state.messages)) throw new Error('Invalid state file');
  const result = await collect({ ...options, state, onSnapshot: (name, html) => writeFile(path.join(dir, name), html, { mode: 0o600 }) });
  const { state: nextState, ...output } = result;
  // Persist output first, so a failed output write does not advance the state cursor.
  const runFile = `events-${Date.now()}.json`;
  await atomicJson(path.join(dir, runFile), output);
  await atomicJson(path.join(dir, 'state.json'), nextState);
  console.log(JSON.stringify({ channel: options.channel, polledMessages: result.polledMessages,
    events: result.events.length, errors: result.errors.length, output: path.join(dir, runFile) }));
  if (result.errors.length) process.exitCode = 2;
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => { console.error(`Source adapter failed: ${error.message}`); process.exitCode = 1; });
}
