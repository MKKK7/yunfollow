"""Shared operations for the authenticated console, owner API and private Bot."""
import asyncio
import time
from datetime import date

from ..core import tmdb
from ..core.contracts import TaskState
from .catalog import Catalog
from .candidates import have_episodes
from .timeline import summary


async def refresh_airing(service):
    for sub in service.store.subscriptions():
        key = f"{sub.get('tmdbid')}:{sub['season']}"
        prior = service.store.state_get('yun_airing', key, {})
        if not sub.get('tmdbid') or time.time() - prior.get('checked_at', 0) < 21600:
            continue
        try:
            with service.pipeline.scope():
                episodes = await asyncio.to_thread(tmdb.season_episodes, sub['tmdbid'], sub['season'], language='zh-CN', timeout=10)
            today = date.today().isoformat()
            aired = sorted({int(e['episode']) for e in episodes if e.get('air_date') and e['air_date'] <= today})
            service.store.state_set('yun_airing', key, {'aired': aired, 'checked_at': time.time(), 'state': 'verified',
                'total': len(episodes), 'unreleased': [int(e['episode']) for e in episodes if e.get('air_date') and e['air_date'] > today]})
        except Exception as exc:
            service.store.state_set('yun_airing', key, {**prior, 'state': 'unknown', 'error': type(exc).__name__, 'checked_at': time.time()})


def subscriptions(service):
    values = []
    for sub in service.store.subscriptions():
        have = have_episodes(service.store, sub)
        airing = service.store.state_get('yun_airing', f"{sub.get('tmdbid')}:{sub['season']}", {})
        expected = set(airing.get('aired', [])) if airing.get('state') == 'verified' else set()
        values.append({**sub, 'have': sorted(have), 'missing': sorted(expected - have),
                       'airing': airing, 'complete_aired': expected.issubset(have) if expected else None,
                       'preferences': service.store.state_get('fnos_subscriptions', f"tmdb:{sub.get('tmdbid')}:S{sub['season']}", {}).get('preferences', {'min_resolution': '1080p', 'subtitles': 'zh', 'replace_existing': False}),
                       'follow': service.store.state_get('yun_follow', f"{sub.get('tmdbid')}:{sub['season']}", {})})
    return values


def tasks(service, waiting=False):
    values = []
    for row in service.list_tasks(limit=200):
        ctx = service.store.get_task(row['task_id'])
        if not ctx or not ctx.raw.extra.get('fnos_subscription'):
            continue
        if waiting and ctx.state not in (TaskState.PENDING_APPROVAL, TaskState.NEED_MANUAL, TaskState.FAILED):
            continue
        values.append({**summary(ctx), 'error': row.get('error'), 'updated_at': row.get('updated_at')})
    return values


def wake(service, source_id=None):
    service.store.state_set('yun_source_actions', source_id or 'force_all', True)
    Catalog(service.store).wake()


async def task_action(service, task_id, action):
    ctx = service.store.get_task(task_id)
    if not ctx or not ctx.raw.extra.get('fnos_subscription'):
        raise ValueError('Workflow task not found')
    if action == 'retry':
        if ctx.state == TaskState.NEED_MANUAL and ctx.ns('storage').get('phase') == 'receiving':
            return await service.reconcile_transfer(task_id)
        if ctx.state == TaskState.NEED_MANUAL and ctx.ns('storage').get('landed'):
            if ctx.ns('yun_classification').get('waiting'):
                raise ValueError('Choose a destination before retrying')
            return await service.requeue_organize(task_id)
        return await service.retry(task_id)
    if action == 'approve':
        # Revalidate an old pending task rather than trusting a stale approval card.
        from .preflight import Preflight
        raw = ctx.raw
        candidate = {'title': ctx.parsed.title, 'share': {'shareCode': ctx.parsed.share_code, 'password': ctx.parsed.receive_code}}
        verdict, reason = await Preflight(service.pipeline).check(raw, candidate, 'manual-task', raw.extra['fnos_subscription'])
        if verdict != 'accepted':
            raise ValueError('Resource recheck failed: ' + reason)
        service.pipeline.stamp_config(ctx)
        service.store.update_task_context(ctx, expected=TaskState.PENDING_APPROVAL)
        return await service.approve(task_id)
    if action == 'ignore':
        return await service.reject(task_id)
    if action == 'verify':
        from .verification import verify
        with service.pipeline.scope(ctx):
            return await verify(ctx, service)
    raise ValueError('Unsupported task operation')


async def test_source(service, identity):
    from .sources import command_json
    from .http import api
    from .kdocs import refresh_documents
    catalog = Catalog(service.store)
    item = next((v for v in catalog.items() if v['id'] == identity), None)
    if not item:
        raise ValueError('Source not found')
    try:
        if item['kind'] == 'kdocs':
            result = await refresh_documents(service.store, source_id=identity)
            if not result or result[0]['state'] != 'ok':
                raise ValueError('Document permission or authentication failed')
        elif item['kind'] == 'pansou':
            r = await api(item['url'] + '/api/health', timeout=15)
            if r.get('code') not in (None, 0, 200) or not r:
                raise ValueError('Search service health check failed')
        else:
            channel = item['url'].rsplit('/', 1)[-1]
            r = await command_json(['node', '/app/fnos-adapters/telegram.mjs', '--channel', channel,
                '--limit', '1', '--snapshot-dir', '/tmp/yunfollow-source-' + identity])
            if r.get('errors'):
                raise ValueError('Channel could not be read')
        catalog.health(identity, 'ok', test_only=True)
    except Exception as exc:
        catalog.health(identity, 'error', error=type(exc).__name__)
    return next(v for v in catalog.public() if v['id'] == identity)
