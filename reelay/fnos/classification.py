"""Explicit destination rules. Ambiguous classification waits in115, never downloads."""
from ..core import runtime
from ..core.registry import register
from .timeline import stage

CATEGORIES = ['电影', '动漫', '综艺', '日剧', '英剧', '美剧', '电视剧']


def category(parsed):
    if parsed.media_type == 'movie' and not parsed.episodes:
        return '电影'
    if parsed.media_type not in ('tv', 'anime') and not parsed.episodes:
        return None
    genres = set(parsed.genre_ids or [])
    if parsed.media_type == 'anime' or 16 in genres:
        return '动漫'
    if genres & {10764, 10767}:
        return '综艺'
    countries = set(parsed.origin_country or [])
    for name, codes in [('日剧', {'JP'}), ('英剧', {'GB', 'UK'}), ('美剧', {'US'}), ('电视剧', {'CN', 'HK', 'TW'})]:
        if countries & codes:
            return name
    return None


@register(kind='hook', stage='before_save', order=15)
class Classify:
    name = 'yun_classification'
    default_enabled = True
    stage, order = 'before_save', 15

    async def run(self, ctx):
        if not ctx.raw.extra.get('fnos_subscription'):
            return True
        if not ctx.meta.get('tmdb', {}).get('matched'):
            raise ValueError('Work identity must be verified before115 transfer')
        value = ctx.ns('yun_classification')
        destination = value.get('confirmed') or category(ctx.parsed)
        if destination and destination not in CATEGORIES:
            raise ValueError('Unknown destination category')
        value.update(destination=destination or '待分类', waiting=not bool(destination))
        ctx.ns('library').update(relative_category=value['destination'], category_path=value['destination'])
        ctx.log('分类规则：' + value['destination'])
        return True


@register(kind='hook', stage='after_save', order=25)
class ClassificationWait:
    name = 'yun_classification'
    default_enabled = True
    stage, order = 'after_save', 25

    async def run(self, ctx):
        if not ctx.raw.extra.get('fnos_subscription'):
            return True
        if ctx.ns('yun_classification').get('waiting'):
            if not ctx.ns('organize').get('applied'):
                return False
            stage(ctx, 'classification', 'waiting_confirmation', directory='待分类', reason='分类不明确，请选择目录')
            ctx.log('等待分类确认：视频已存放在115待分类目录，尚未刮削')
            runtime.services().store.stage_notice('classify-' + ctx.task_id,
                f'《{ctx.parsed.title}》已在115待分类目录，选择目标分类后继续。', ['fnos_bot'],
                buttons=[[{'text': name, 'callback_data': 'folder:' + ctx.task_id + ':' + str(i)}]
                         for i, name in enumerate(CATEGORIES)])
            return False
        stage(ctx, 'classification', 'done', destination=ctx.ns('library').get('relative_category'))
        return True


async def choose(service, task_id, destination):
    from ..core.contracts import TaskState
    if destination not in CATEGORIES:
        raise ValueError('Choose an existing category')
    ctx = service.store.get_task(task_id)
    if not ctx or ctx.state != TaskState.NEED_MANUAL or not ctx.ns('yun_classification').get('waiting'):
        raise ValueError('Task is not waiting for classification')
    ctx.ns('yun_classification').update(confirmed=destination, destination=destination, waiting=False)
    ctx.ns('library').update(relative_category=destination, category_path=destination)
    ctx.ns('organize')['applied'] = False
    service.store.update_task_context(ctx, expected=TaskState.NEED_MANUAL)
    await service.requeue_organize(task_id)
    return {'state': 'queued', 'destination': destination}
