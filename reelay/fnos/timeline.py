"""Task stages derived from native state plus explicit existing-service receipts."""
import time

from ..core import runtime

STAGES = [('search', '搜索'), ('validate', '分享/剧集核验'), ('transfer', '115云端转存'),
          ('organize', '云端整理'), ('strm', 'LitePan STRM'), ('scrape', 'LitePan刮削'),
          ('writeback', '元数据回写'), ('emby', 'LitePan通知Emby')]


def stage(ctx, name, state, **receipt):
    ctx.ns('fnos_workflow').setdefault('stages', {})[name] = {
        'state': state, 'updated_at': time.time(), 'receipt': receipt}
    runtime.services().store.save_task(ctx)


def summary(ctx):
    saved = ctx.meta.get('fnos_workflow', {}).get('stages', {})
    storage = ctx.meta.get('storage', {})
    phase = ctx.state.value
    derived = {'search': 'done', 'validate': 'done' if ctx.meta.get('fnos_workflow', {}).get('preflight') else 'pending',
               'transfer': 'done' if storage.get('phase') == 'stored' else ('running' if phase == 'saving' else 'pending'),
               'organize': 'done' if ctx.meta.get('organize', {}).get('applied') else ('running' if phase == 'organizing' else 'pending')}
    blockers = [line for line in ctx.trail if any(term in line for term in ('FNOS：', '未完成', '异常', '失败'))]
    stages = [{'id': key, 'label': label, **saved.get(key, {'state': derived.get(key, 'pending')})} for key, label in STAGES]
    if phase in ('need_manual', 'failed'):
        stages = [{**s, 'state': 'failed'} if s['state'] == 'running' else s for s in stages]
    return {'task_id': ctx.task_id, 'title': ctx.parsed.title if ctx.parsed else ctx.raw.extra.get('fnos_subscription', {}).get('title'),
            'state': phase, 'blocker': blockers[-1] if blockers else None,
            'stages': stages}
