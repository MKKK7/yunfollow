"""Task stages derived from native state plus explicit existing-service receipts."""
import time
import logging

from ..core import runtime

STAGES = [('intent', '需求识别'), ('work', '作品确认'), ('search', '搜索'),
          ('link', '链接核验'), ('validate', '实际文件/季集核验'), ('selection', '资源选择'),
          ('transfer', '115云端转存/离线下载'), ('classification', '分类确认'),
          ('organize', '云端整理'), ('strm', 'LitePan STRM'), ('scrape', 'LitePan刮削'),
          ('writeback', '元数据回写'), ('emby', 'LitePan通知Emby'), ('verify', '结果核验')]

log = logging.getLogger('yunfollow.workflow')


def stage(ctx, name, state, **receipt):
    previous = ctx.ns('fnos_workflow').setdefault('stages', {}).get(name, {})
    now = time.time()
    ctx.ns('fnos_workflow').setdefault('stages', {})[name] = {
        'state': state, 'updated_at': now, 'started_at': previous.get('started_at', now),
        'finished_at': now if state in ('done', 'failed', 'skipped') else None, 'receipt': receipt}
    provider = receipt.get('provider', name)
    message = f'[{ctx.task_id}][{name}] {provider}：' + {'running': '开始调用', 'done': '调用完成并记录回执', 'failed': '调用失败', 'waiting_confirmation': '等待用户确认'}.get(state, state)
    log.info('%s', message)
    ctx.log(message)
    runtime.services().store.save_task(ctx)


def summary(ctx):
    saved = ctx.meta.get('fnos_workflow', {}).get('stages', {})
    storage = ctx.meta.get('storage', {})
    phase = ctx.state.value
    checked = bool(ctx.meta.get('fnos_workflow', {}).get('preflight'))
    derived = {'intent': 'done', 'work': 'done' if checked else 'pending', 'link': 'done' if checked else 'pending',
               'selection': ('waiting_confirmation' if phase == 'pending_approval' else 'done') if ctx.parsed else 'pending',
               'classification': 'waiting_confirmation' if ctx.meta.get('yun_classification', {}).get('waiting') else 'pending',
               'search': 'done', 'validate': 'done' if checked else 'pending',
               'transfer': 'done' if storage.get('phase') == 'stored' else ('running' if phase == 'saving' else 'pending'),
               'organize': 'done' if ctx.meta.get('organize', {}).get('applied') else ('running' if phase == 'organizing' else 'pending')}
    blockers = [line for line in ctx.trail if any(term in line for term in ('FNOS：', '未完成', '异常', '失败'))]
    stages = [{'id': key, 'label': label, **saved.get(key, {'state': derived.get(key, 'pending')})} for key, label in STAGES]
    if phase in ('need_manual', 'failed'):
        stages = [{**s, 'state': 'failed'} if s['state'] == 'running' else s for s in stages]
    if ctx.meta.get('yun_classification', {}).get('waiting'):
        stages = [{**s, 'state': 'waiting_confirmation'} if s['id'] == 'classification' else s for s in stages]
    return {'task_id': ctx.task_id, 'title': ctx.parsed.title if ctx.parsed else ctx.raw.extra.get('fnos_subscription', {}).get('title'),
            'state': phase, 'blocker': blockers[-1] if blockers else None,
            'stages': stages, 'trail': ctx.trail[-80:], 'episodes': ctx.parsed.episodes if ctx.parsed else [],
            'directories': sorted({p['target_dir'] for p in ctx.meta.get('organize', {}).get('plan', [])}),
            'classification': ctx.meta.get('yun_classification', {})}
