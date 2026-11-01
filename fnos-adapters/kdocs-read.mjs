import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const DEFAULT_URL = '';
const PROJECT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MAX_OUTPUT = 16 * 1024 * 1024;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class ReadError extends Error {
  constructor(status, code) {
    super(`KDocs read stopped: ${status}`);
    this.name = 'ReadError';
    this.status = status;
    this.code = code;
  }
}

export function validateUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new ReadError('invalid_share_url'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port
      || !['www.kdocs.cn', 'kdocs.cn', '365.kdocs.cn'].includes(url.hostname)
      || !/^\/l\/[A-Za-z0-9]+\/?$/.test(url.pathname)) {
    throw new ReadError('invalid_share_url');
  }
  // Tracking/query parameters are unnecessary for the one authorized share.
  url.search = '';
  url.hash = '';
  return url.toString().replace(/\/$/, '');
}

function classifyFailure(value) {
  const code = value?.code ?? value?.statusCode;
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? {});
  if (/(企业账[号户]|企业账户|enterprise|403001)/i.test(text)) return new ReadError('enterprise_account_unsupported', code);
  if (['403', '403001'].includes(String(code)) || /\b403\b|forbidden|permission denied|权限不足|无权访问|无权限/i.test(text)) return new ReadError('permission_denied', code);
  if (['401', '400006'].includes(String(code)) || /\b401\b|unauthorized|鉴权失败|token.{0,20}(expired|过期)|未登录/i.test(text)) return new ReadError('authentication_required', code);
  if (['429', '429001', '429002'].includes(String(code)) || /\b429\b|限频|熔断/i.test(text)) return new ReadError('rate_limited', code);
  return new ReadError('cli_or_service_error', code);
}

/** Run only read-file. Secrets come from the CLI keychain, never command arguments. */
export function runCliRead(cli, payload) {
  return new Promise((resolve, reject) => {
    const child = spawn(cli, ['drive', 'read-file', '-', '--compact', '--timeout', '30000'], {
      shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    let outputBytes = 0;
    let rejected = false;
    const timer = setTimeout(() => {
      rejected = true;
      child.kill();
      reject(new ReadError('cli_timeout'));
    }, 45000);
    const collect = (stream) => (chunk) => {
      outputBytes += Buffer.byteLength(chunk, 'utf8');
      if (outputBytes > MAX_OUTPUT) {
        rejected = true;
        child.kill();
        reject(new ReadError('response_too_large'));
      } else if (stream === 'out') stdout += chunk.toString('utf8');
      else stderr += chunk.toString('utf8');
    };
    // StringDecoder behaviour is provided by setEncoding for split UTF-8 chunks.
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', collect('out'));
    child.stderr.on('data', collect('err'));
    child.on('error', () => {
      clearTimeout(timer);
      rejected = true;
      reject(new ReadError('cli_unavailable'));
    });
    child.stdin.on('error', () => {}); // Process error/exit carries the safe result.
    child.on('close', (exitCode) => {
      clearTimeout(timer);
      if (rejected) return;
      let result;
      try { result = JSON.parse(stdout.trim().replace(/^\uFEFF/, '')); }
      catch { reject(classifyFailure(stderr || stdout)); return; }
      if (exitCode !== 0) { reject(classifyFailure(result)); return; }
      resolve(result);
    });
    child.stdin.end(JSON.stringify(payload), 'utf8');
  });
}

function allStrings(value) {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(allStrings);
  if (value && typeof value === 'object') return Object.values(value).flatMap(allStrings);
  return [];
}

/** Candidates only: no share opening, transfer, or download occurs. */
export function extractCandidates(content) {
  const texts = allStrings(content);
  const shares = new Map();
  const tmdb = new Map();
  const offline = new Map();
  for (const text of texts) {
    const normalized = text.replace(/&amp;/g, '&');
    for (const match of normalized.matchAll(/magnet:\?[^\s<>"']+|ed2k:\/\/\|file\|[^|\s]+\|\d+\|[a-fA-F0-9]{32}\|\/?/gi)) {
      if (match[0].length > 4096) continue;
      offline.set(match[0], { uri: match[0], contextSnippet: normalized.slice(Math.max(0, match.index - 160), match.index).replace(/https?:\/\/\S+/g, '[link]').slice(-160) });
    }
    for (const match of normalized.matchAll(/https?:\/\/(?:www\.)?(?:115\.com|115cdn\.com)\/s\/[A-Za-z0-9]+(?:\?[^\s<>"']*)?/gi)) {
      const raw = match[0].replace(/[)\]}>，。；;]+$/g, '');
      const url = new URL(raw);
      const shareCode = url.pathname.split('/').at(-1);
      const canonical = `https://115.com/s/${shareCode}`;
      const tail = normalized.slice(match.index + match[0].length, match.index + match[0].length + 80).split('\n')[0];
      const password = url.searchParams.get('password') ?? url.searchParams.get('pwd')
        ?? /(?:提取码|访问码|密码)\s*[:：=]?\s*([A-Za-z0-9]{4,8})/.exec(tail)?.[1] ?? null;
      const safePassword = password && /^[A-Za-z0-9]{4,8}$/.test(password) ? password : null;
      const lineStart = normalized.lastIndexOf('\n', match.index - 1) + 1;
      const contextSnippet = normalized.slice(Math.max(lineStart, match.index - 160), match.index)
        .replace(/https?:\/\/\S+/g, '[link]').replace(/[\t\r ]+/g, ' ').trim().slice(-160);
      const prior = shares.get(shareCode);
      shares.set(shareCode, {
        url: canonical, shareCode, password: safePassword ?? prior?.password ?? null,
        contextSnippet: contextSnippet || prior?.contextSnippet || '', candidate: true,
      });
    }
    for (const match of normalized.matchAll(/(?:themoviedb\.org\/(movie|tv)\/|tmdb\s*[-_:：=]?\s*)(\d{1,10})/gi)) {
      const kind = match[1] ?? null;
      tmdb.set(`${kind}:${match[2]}`, { id: match[2], mediaType: kind, candidate: true });
    }
  }
  return { shares115: [...shares.values()], tmdb: [...tmdb.values()], offlineLinks: [...offline.values()].slice(0, 100) };
}

export async function readDocument({ url = DEFAULT_URL, cli, maxPolls = 8, pollMs = 2000, run = runCliRead, sleep = delay } = {}) {
  const shareUrl = validateUrl(url);
  const original = { url: shareUrl };
  let payload = { ...original };
  for (let attempt = 0; attempt <= maxPolls; attempt++) {
    const response = await run(cli, payload);
    if (!response || typeof response !== 'object' || String(response.code) !== '0') throw classifyFailure(response);
    const data = response.data;
    if (!data || typeof data !== 'object') throw new ReadError('invalid_response');
    if (data.status === 'pending') {
      if (typeof data.task_id !== 'string' || !data.task_id.trim()) throw new ReadError('pending_without_task_id');
      if (attempt === maxPolls) throw new ReadError('pending_timeout');
      payload = { ...original, task_id: data.task_id };
      await sleep(pollMs);
      continue;
    }
    if (data.status !== 'ok' || !Object.hasOwn(data, 'content')) throw new ReadError('invalid_response');
    const warnings = Array.isArray(data.warnings) ? data.warnings.filter((x) => typeof x === 'string') : [];
    const spreadsheet = /\.(xlsx?|et|ksheet|dbt)$/i.test(String(data.suffix ?? '')) || /sheet|records/i.test(String(data.content_format ?? ''));
    const document = Object.fromEntries(['file_id', 'drive_id', 'name', 'suffix', 'content_format', 'size']
      .filter((key) => Object.hasOwn(data, key)).map((key) => [key, data[key]]));
    return {
      source: { provider: 'kdocs', url: shareUrl, fetchedAt: new Date().toISOString() },
      status: 'ok',
      completeness: { status: warnings.length || spreadsheet ? 'uncertain' : 'not_reported_partial', warnings, spreadsheet, note: 'Absence of warnings is not proof of completeness.' },
      document,
      contentHash: createHash('sha256').update(JSON.stringify(data.content), 'utf8').digest('hex'),
      contentPersisted: false,
      candidates: extractCandidates(data.content),
      requests: attempt + 1,
    };
  }
  throw new ReadError('pending_timeout');
}

export function parseArgs(args) {
  const options = {
    url: DEFAULT_URL,
    cli: path.join(PROJECT_ROOT, 'tools', 'kdocs-cli', 'bin', process.platform === 'win32' ? 'kdocs-cli.exe' : 'kdocs-cli'),
    snapshotDir: path.join(PROJECT_ROOT, 'source-snapshots', 'kdocs'),
  };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--help') return { help: true };
    const key = { '--url': 'url', '--cli': 'cli', '--snapshot-dir': 'snapshotDir' }[args[i]];
    if (!key || !args[i + 1] || args[i + 1].startsWith('--')) throw new ReadError('invalid_arguments');
    options[key] = args[++i];
  }
  options.url = validateUrl(options.url);
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log('node source-adapters/kdocs-read.mjs [--url https://www.kdocs.cn/l/ID] [--snapshot-dir DIR] [--cli PATH]');
    return;
  }
  const snapshot = await readDocument(options);
  await mkdir(options.snapshotDir, { recursive: true, mode: 0o700 });
  const id = createHash('sha256').update(snapshot.source.url).digest('hex').slice(0, 12);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const file = path.join(options.snapshotDir, `${id}-${stamp}.json`);
  await writeFile(file, JSON.stringify(snapshot, null, 2) + '\n', { encoding: 'utf8', mode: 0o600, flag: 'wx' });
  console.log(JSON.stringify({
    status: 'ok', snapshot: file, requests: snapshot.requests,
    completeness: snapshot.completeness.status, warningCount: snapshot.completeness.warnings.length,
    document: snapshot.document, contentHash: snapshot.contentHash, contentPersisted: false,
    candidates115: snapshot.candidates.shares115.length, candidatesTMDB: snapshot.candidates.tmdb.length,
    executedTransfers: 0,
  }));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(JSON.stringify({ status: error instanceof ReadError ? error.status : 'local_execution_error', code: error instanceof ReadError ? error.code : undefined }));
    process.exitCode = 1;
  });
}
