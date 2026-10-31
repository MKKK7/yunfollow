// Reuse an already authorized official CLI; never export its account token.
import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { readDocument } from './kdocs-read.mjs';

export async function syncOnce(config, { read = readDocument, http = fetch } = {}) {
  const base = new URL(config.baseUrl);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || String(config.key).length < 24) throw new Error('Invalid bridge configuration');
  async function request(route, body) {
    const response = await http(new URL('/fnos/v1/' + route, base), {
      method: body ? 'POST' : 'GET', redirect: 'error', signal: AbortSignal.timeout(30000),
      headers: { Authorization: 'Bearer ' + config.key, 'Content-Type': 'application/json' },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new Error('Bridge API rejected request');
    const text = await response.text();
    if (Buffer.byteLength(text) > 2 * 1024 * 1024) throw new Error('Bridge response too large');
    return JSON.parse(text);
  }
  const sources = await request('sources');
  const results = [];
  for (const source of sources.filter((s) => s.enabled && s.kind === 'kdocs')) {
    try {
      const snapshot = await read({ url: source.url, cli: config.cli });
      const receipt = await request(`sources/${source.id}/snapshot`, {
        source: snapshot.source, status: snapshot.status, contentPersisted: false,
        contentHash: snapshot.contentHash, completeness: snapshot.completeness,
        candidates: { shares115: snapshot.candidates.shares115 },
      });
      results.push({ source_id: source.id, state: 'ok', candidates: receipt.candidates });
    } catch (error) {
      const code = ({ authentication_required: 'auth_error', enterprise_account_unsupported: 'permission_denied',
        auth_error: 'auth_error', permission_denied: 'permission_denied', rate_limited: 'rate_limited' })[error.status] ?? 'sync_error';
      try { await request(`sources/${source.id}/sync-status`, { state: code }); } catch { /* Next successful call reconciles health. */ }
      results.push({ source_id: source.id, state: code });
    }
  }
  return { finished_at: new Date().toISOString(), results };
}

async function main() {
  const configPath = process.argv[2];
  if (!configPath) throw new Error('Pass a private bridge config path');
  const config = JSON.parse(await readFile(configPath, 'utf8'));
  const result = await syncOnce(config);
  if (config.statusFile) await writeFile(config.statusFile, JSON.stringify(result, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(result));
  if (result.results.some((r) => r.state !== 'ok')) process.exitCode = 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(() => { console.error('Document bridge failed; credentials and document bodies are not logged.'); process.exitCode = 1; });
}
