"""115 server-side offline jobs. Never retrieves torrents or video bytes locally."""
import asyncio
import base64
import hashlib
import re
import time
from urllib.parse import parse_qs, urlsplit

from ..core import paths
from ..core.contracts import ParsedResource, PipelineContext, RawMessage, TaskState
from ..core.media import episode_of, is_extra, is_video
from .candidates import have_episodes


def identity(uri):
    if len(uri) > 4096:
        raise ValueError('Offline link too long')
    if uri.startswith('magnet:?'):
        values = parse_qs(urlsplit(uri).query).get('xt', [])
        digest = next((v[9:] for v in values if v.lower().startswith('urn:btih:')), '')
        if re.fullmatch('[a-fA-F0-9]{40}', digest):
            return digest.lower()
        if re.fullmatch('[A-Z2-7a-z]{32}', digest):
            return base64.b32decode(digest.upper()).hex()
    if re.fullmatch(r'ed2k://\|file\|[^|/]+\|[1-9][0-9]*\|[a-fA-F0-9]{32}\|/?', uri):
        return uri.split('|')[4].lower()
    raise ValueError('Only valid magnet BTIH or ed2k file links can be sent to115')


def create(service, uri, subscription_id):
    digest = identity(uri)
    sub = next((s for s in service.store.subscriptions() if s['id'] == subscription_id), None)
    if not sub or not sub.get('tmdbid'):
        raise ValueError('Confirm the target subscription first')
    key = hashlib.sha256(f"{digest}:{sub['tmdbid']}:{sub['season']}".encode()).hexdigest()[:20]
    existing = service.store.state_get('yun_offline', key)
    if existing:
        return public(existing)
    # Draft only; the explicit confirm button authorizes115 side effects.
    value = {'id': key, 'uri': uri, 'hash': digest, 'subscription': sub, 'state': 'waiting_confirmation',
             'created_at': time.time(), 'updated_at': time.time(), 'video_downloaded_locally': False}
    service.store.state_set('yun_offline', key, value)
    return public(value)


def public(value):
    return {k: v for k, v in value.items() if k not in ('uri', 'hash', 'response')}


def jobs(store):
    return [public(v) for v in store.state_items('yun_offline').values()]


def notify_drafts(store):
    active = store.state_get('yun_offline_notice', 'active', [])
    if any(store.state_get('yun_offline', key, {}).get('state') == 'waiting_confirmation' for key in active):
        return
    values = [v for v in store.state_items('yun_offline').values() if v.get('state') == 'waiting_confirmation' and not v.get('notified')][:3]
    for value in values:
        store.stage_notice('offline-draft-' + value['id'],
            f"{value['subscription']['title']} · 115离线草稿\n来源：{value.get('source_name') or '现有来源'}\n{value.get('evidence') or ''}\n尚未证明可下载，需要115实际反馈；不会在NAS下载中转。",
            ['fnos_bot'], buttons=[[{'text': '确认交给115云端离线', 'callback_data': 'offline:confirm:' + value['id']}]])
        value['notified'] = True
        store.state_set('yun_offline', value['id'], value)
    if values:
        store.state_set('yun_offline_notice', 'active', [v['id'] for v in values])


def confirm(service, key):
    value = service.store.state_get('yun_offline', key)
    if not value:
        raise ValueError('Offline job not found')
    if value['state'] != 'waiting_confirmation':
        return public(value)
    value.update(state='queued', confirmed_at=time.time(), updated_at=time.time())
    service.store.state_set('yun_offline', key, value)
    return public(value)


def _save(store, value, **fields):
    value.update(updated_at=time.time(), **fields)
    store.state_set('yun_offline', value['id'], value)


def _walk(drive, parent, depth=0):
    if depth > 4:
        raise ValueError('Offline directory too deep; manual review required')
    out = []
    for attr in drive.iterdir(int(parent)):
        if attr.get('is_dir'):
            out.extend(_walk(drive, attr['id'], depth + 1))
        elif is_video(attr['name']) and not is_extra(attr['name']):
            out.append(attr)
        if len(out) > 200:
            raise ValueError('Offline job exceeds200 videos; manual review required')
    return out


async def tick(service, value):
    """One offline worker process owns writes; unknown submissions are never blindly repeated."""
    store, pipeline = service.store, service.pipeline
    pipeline.reload_config()
    storage = pipeline.reg.get_provider('storage', 'p115')
    drive = storage.library()
    client = storage._cookie_client()
    root = int(pipeline.cfg['save_dirs'][pipeline.cfg['defaults']['save_to']])
    if value['state'] == 'queued':
        # Named stage makes a crash after mkdir recoverable without creating another directory.
        name = '.yun-offline-' + value['id']
        entries = await asyncio.to_thread(drive.iterdir, root)
        existing = next((x for x in entries if x['name'] == name and x['is_dir']), None)
        cid = existing['id'] if existing else await asyncio.to_thread(drive.mkdir, root, name)
        _save(store, value, state='submitting', stage_cid=str(cid))
        try:
            result = await asyncio.to_thread(client.clouddownload_task_add_url, {'url': value['uri'], 'wp_path_id': cid}, timeout=20)
            if not result.get('state'):
                _save(store, value, state='rejected', reason='115明确拒绝离线任务')
                return
            _save(store, value, state='downloading', accepted=True)
        except Exception as exc:
            _save(store, value, state='submission_unknown', reason=type(exc).__name__)
            return
    if value['state'] in ('submitting', 'submission_unknown', 'downloading'):
        task = None
        for page in range(1, 4):
            result = await asyncio.to_thread(client.clouddownload_task_list, page, timeout=15)
            data = result.get('tasks') or (result.get('data') if isinstance(result.get('data'), list) else []) or []
            task = next((x for x in data if str(x.get('info_hash', '')).lower() == value['hash']), None)
            if task or not data:
                break
        if not task:
            _save(store, value, state='submission_unknown', reason='115任务记录未找到；仅核验，不重新提交')
            return
        status = int(task.get('status', 0))
        if status < 0:
            _save(store, value, state='failed', reason='115云端离线下载失败')
            return
        _save(store, value, percent=float(task.get('percentDone') or 0), state='downloading')
        if status != 2:
            return
        files = await asyncio.to_thread(_walk, drive, value['stage_cid'])
        if not files:
            _save(store, value, state='needs_review', reason='离线完成但未发现可核验的正片视频')
            return
        _save(store, value, state='waiting_file_confirmation', files=[
            {'id': str(x['id']), 'name': x['name'], 'size': x['size'], 'sha1': x.get('sha1'),
             'season': episode_of(x['name'])[0], 'episode': episode_of(x['name'])[1]} for x in files])
        store.stage_notice('offline-files-' + value['id'], f"115离线完成：{value['subscription']['title']}，实际{len(files)}个视频。请在离线任务卡片核对实际季集后继续整理。", ['fnos_bot'],
            buttons=[[{'text': '核对实际文件', 'callback_data': 'offline:files:' + value['id']}]])


async def ingest(service, key):
    store = service.store
    value = store.state_get('yun_offline', key)
    if value and value.get('task_id'):
        return {'task_id': value['task_id'], 'state': 'already_ingested'}
    if not value or value['state'] != 'waiting_file_confirmation':
        raise ValueError('Wait for115 offline download and confirm actual files first')
    sub = value['subscription']
    # The user confirms the work; filenames must still deterministically prove season/episode.
    have = have_episodes(store, sub)
    selected = [f for f in value['files'] if f['episode'] is not None and f['season'] in (None, sub['season']) and f['episode'] not in have]
    if not selected:
        raise ValueError('No verifiable missing episodes in downloaded files; nothing moved')
    from ..core import tmdb, works
    with service.pipeline.scope():
        info = await asyncio.to_thread(tmdb.tv_details, sub['tmdbid'], language='zh-CN', timeout=10)
    if info.get('id') != sub['tmdbid']:
        raise ValueError('TMDB identity verification failed')
    episodes = sorted({f['episode'] for f in selected})
    raw = RawMessage(source='@yun_offline', msg_id=key, text='User-confirmed115 cloud offline result', extra={'fnos_subscription': sub, 'yun_resource_confirmed': True})
    ctx = PipelineContext(raw=raw, task_id=hashlib.sha256(('offline:' + key).encode()).hexdigest()[:12], state=TaskState.STORED,
        parsed=ParsedResource(is_media=True, media_type='anime' if 16 in info.get('genre_ids', []) else 'tv',
            title=sub['title'], season=sub['season'], year=int(info['first_air_date'][:4]) if info.get('first_air_date') else None,
            episodes=episodes, genre_ids=info.get('genre_ids', []), confidence=1, raw_fields={'tmdbid': sub['tmdbid']}))
    ctx.source_rule = service.pipeline.rule_for(raw.source)
    ctx.save_dir_alias = service.pipeline.cfg['defaults']['save_to']
    ctx.save_dir_cid = str(service.pipeline.cfg['save_dirs'][ctx.save_dir_alias])
    pairs = [[sub['season'], e] for e in episodes]
    ctx.meta = {'tmdb': {'matched': True, 'tmdbid': sub['tmdbid'], 'kind': 'tv'},
        'fnos_workflow': {'preflight': {'state': 'done', 'reason': '115 offline actual files plus explicit owner confirmation'}},
        'storage': {'provider': 'p115', 'phase': 'stored', 'landed': selected,
                    'manifest': {'to_cid': int(ctx.save_dir_cid), 'staging_cid': int(value['stage_cid']), 'files': selected,
                                 'episodes': episodes, 'season': sub['season'], 'pairs': pairs},
                    'identity': {'provider': 'p115', 'cid': ctx.save_dir_cid, 'share_code': 'offline:' + value['hash'],
                                 'title': sub['title'], 'season': sub['season'], 'episodes': episodes, 'pairs': pairs,
                                 'work': works.of(ctx.parsed, {'tmdbid': sub['tmdbid'], 'kind': 'tv'})}}}
    ctx.result = {'verified': True, 'storage_verified': True, 'via': '115_offline'}
    service.pipeline.stamp_config(ctx)
    if not await service.pipeline.run_hooks('before_save', ctx):
        raise ValueError('Classification check did not pass')
    # Deterministic ID and a database transaction serialize web/Bot confirmations.
    if not store.get_task(ctx.task_id):
        keys = service.pipeline._content_keys('p115', ctx)
        if not store.claim_resource(ctx, 'p115:' + ctx.save_dir_cid + ':offline:' + value['hash'], content_keys=keys):
            raise ValueError('Another task is already processing these episodes; wait and recheck')
    _save(store, value, state='ingested', task_id=ctx.task_id)
    return {'task_id': ctx.task_id, 'state': 'stored'}


async def run():
    from types import SimpleNamespace
    from ..core.config import load_config, normalize_config
    from ..core.registry import discover
    from ..core.store import Store
    from ..core.pipeline import Pipeline
    paths.use_data_dir(paths.DATA)
    store = Store(str(paths.DATABASE))
    pipeline = Pipeline(discover(), store, normalize_config(load_config()))
    service = SimpleNamespace(store=store, pipeline=pipeline)
    try:
        while True:
            for value in list(store.state_items('yun_offline').values()):
                if value.get('state') not in ('queued', 'submitting', 'submission_unknown', 'downloading'):
                    continue
                try:
                    with pipeline.scope():
                        await tick(service, value)
                except Exception as exc:
                    _save(store, value, reason=type(exc).__name__)
            await asyncio.sleep(30)
    finally:
        store.close()
