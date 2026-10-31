import test from 'node:test';
import assert from 'node:assert/strict';
import { collect } from '../fnos-adapters/telegram.mjs';
import { syncOnce } from '../fnos-adapters/kdocs-bridge.mjs';

const channel = '<div class="tgme_widget_message" data-post="examplechannel/1"><div class="tgme_widget_message_text">Test Show <a href="https://telegra.ph/example-show">资源</a></div></div>';
const article = '<article><h1>Test Show</h1><p>TMDB ID:123</p><a href="https://115.com/s/example123?password=abcd">资源</a></article>';

test('public channel search uses an encoded query and returns current observations even without new events', async () => {
  const urls = [];
  const options = { channel: 'examplechannel', search: '测试 剧集', state: { version: 1, messages: {} },
    getHtml: async (url) => { urls.push(url); return url.includes('telegra.ph') ? article : channel; } };
  const first = await collect(options);
  const second = await collect({ ...options, state: first.state });
  assert.match(urls[0], /\?q=%/);
  assert.equal(first.events.length, 1);
  assert.equal(second.events.length, 0);
  assert.equal(second.observations[0].articles[0].shares[0].shareCode, 'example123');
});

test('a login/landing page is not reported as a successful empty search', async () => {
  await assert.rejects(collect({ channel: 'examplechannel', search: 'test', getHtml: async () => '<html>Login</html>' }));
  const empty = await collect({ channel: 'examplechannel', search: 'test', getHtml: async () => '<div class="tgme_channel_info">Public channel</div>' });
  assert.equal(empty.observations.length, 0);
});

test('document bridge reads only configured documents and pushes candidates without raw content or auth token', async () => {
  const sent = [];
  const result = await syncOnce({ baseUrl: 'http://nas.test:28080', key: 'x'.repeat(32), cli: 'official-cli' }, {
    http: async (url, options) => {
      sent.push({ url: String(url), options });
      return { ok: true, text: async () => JSON.stringify(options.method === 'GET'
        ? [{ id: 'doc1', kind: 'kdocs', enabled: true, url: 'https://www.kdocs.cn/l/ExampleDoc' }]
        : { candidates: 1 }) };
    },
    read: async ({ url }) => ({ source: { url }, status: 'ok', contentPersisted: false, contentHash: 'a'.repeat(64),
      content: 'RAW_DOCUMENT_BODY', accountToken: 'PRIVATE_ACCOUNT_TOKEN',
      candidates: { shares115: [{ shareCode: 'example123', contextSnippet: 'Test Show' }] } }),
  });
  assert.equal(result.results[0].state, 'ok');
  assert.equal(sent.length, 2);
  assert.ok(!sent[1].options.body.includes('RAW_DOCUMENT_BODY'));
  assert.ok(!sent[1].options.body.includes('PRIVATE_ACCOUNT_TOKEN'));
  assert.equal(sent[1].options.redirect, 'error');
});

test('document permission failures are reported without retrying the read', async () => {
  let reads = 0;
  const posts = [];
  const result = await syncOnce({ baseUrl: 'http://nas.test', key: 'x'.repeat(32) }, {
    http: async (url, options) => {
      if (options.method === 'POST') posts.push(JSON.parse(options.body));
      return { ok: true, text: async () => JSON.stringify(options.method === 'GET'
        ? [{ id: 'doc1', kind: 'kdocs', enabled: true, url: 'https://www.kdocs.cn/l/ExampleDoc' }] : {}) };
    },
    read: async () => { reads++; throw Object.assign(new Error('must not leak'), { status: 'authentication_required' }); },
  });
  assert.equal(reads, 1);
  assert.equal(posts[0].state, 'auth_error');
  assert.equal(result.results[0].state, 'auth_error');
});
