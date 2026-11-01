import { expect, test } from '@playwright/test';

const stages = ['需求识别', '作品确认', '搜索', '链接核验', '实际文件/季集核验', '资源选择', '115云端转存/离线下载', '分类确认', '云端整理', 'LitePan STRM', 'LitePan刮削', '元数据回写', 'LitePan通知Emby', '结果核验'];
const source = { id: 'source1', name: '动画频道', note: '日常更新', kind: 'telegram', url: 'https://t.me/examplechannel', enabled: true, interval_seconds: 600, health: { state: 'ok', count: 5 } };

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    let data: unknown = {};
    if (path === '/api/auth/status') data = { auth_required: false, authenticated: true };
    if (path === '/api/dashboard') data = { counts: {}, runtime: { worker: { status: 'running', stale: false } }, version: '0.3.0' };
    if (path === '/api/yunfollow/overview') data = {
      name: '云追 YunFollow', version: '0.3.0', categories: ['电影', '动漫', '美剧'], search: { message: '等待用户选择115资源' },
      subscriptions: [], offline: [], integrations: { readiness: { services: { LitePan: { ready: true } } } },
      tasks: [{ task_id: 'a12345678901', title: '测试作品', state: 'need_manual', episodes: [1, 2], directories: ['待分类/测试作品 (2024)/Season 01'], classification: { waiting: true }, trail: ['已调用LitePan'], stages: stages.map((label, index) => ({ id: String(index), label, state: index < 7 ? 'done' : index === 7 ? 'waiting_confirmation' : 'pending' })) }],
    };
    if (path === '/api/yunfollow/sources') data = route.request().method() === 'GET' ? [source] : source;
    if (path === '/api/yunfollow/candidates') data = { id: 'batch1', items: [1, 2, 3].map((number) => ({ id: 'choice' + number, title: '候选作品' + number, year: 2024, season: 1, media_type: 'anime', episodes: [9], new_episodes: [9], overlap: [], missing: [10], resolution: null, release: {}, bytes: 123456789, files: [{ name: 'Test.S01E09.mkv', size: 123456789 }], source: '动画频道', checked_at: 1700000000, share_url: 'https://115cdn.com/s/example' + number })) };
    if (path === '/api/yunfollow/batches/batch1/next') data = { id: 'batch2', items: [] };
    await route.fulfill({ json: data });
  });
});

test('all stages remain visible and classification choice sends a scoped action', async ({ page }) => {
  await page.goto('/workflow/tasks');
  await expect(page.getByRole('heading', { name: '任务流程', exact: true })).toBeVisible();
  for (const label of stages) await expect(page.locator('summary').filter({ hasText: label }).first()).toBeVisible();
  const request = page.waitForRequest((r) => r.url().endsWith('/api/yunfollow/tasks/a12345678901/action') && r.method() === 'POST');
  await page.getByRole('button', { name: '放入动漫', exact: true }).click();
  expect((await request).postDataJSON()).toEqual({ action: 'classify', destination: '动漫' });
});

test('three candidate cards show unknown quality and next drops only this batch', async ({ page }) => {
  await page.goto('/workflow/pending');
  await page.getByRole('button', { name: '查看本批候选', exact: true }).click();
  await expect(page.getByRole('heading', { name: '已核验的115资源 · 本批 3 个' })).toBeVisible();
  await expect(page.getByRole('button', { name: '选择此资源', exact: true })).toHaveCount(3);
  await expect(page.getByText('未知', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '忽略本批，下一批' }).click();
  await expect(page.getByText('暂无符合要求的有效115候选。稍后继续搜索；不会用其他网盘结果代替。')).toBeVisible();
});

test('source name, notes and interval can be edited without changing its URL', async ({ page }) => {
  await page.goto('/workflow/sources');
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  await page.getByLabel('名称', { exact: true }).fill('我的追更频道');
  await page.getByLabel('备注', { exact: true }).fill('只看动画');
  const request = page.waitForRequest((r) => r.url().endsWith('/api/yunfollow/sources/source1') && r.method() === 'PATCH');
  await page.getByRole('button', { name: '保存来源' }).click();
  expect((await request).postDataJSON()).toEqual({ name: '我的追更频道', note: '只看动画', interval_seconds: 600 });
});
