"""Durable three-choice batches. Only revalidated 115 shares can be selected."""
import asyncio
import hashlib
import secrets
import time
from dataclasses import asdict

from ..core import works
from ..core.contracts import PipelineContext, RawMessage
from .preflight import Preflight


def have_episodes(store, target):
    return works.episodes(store.stored_series(), works.identity(target['title'], tmdbid=target['tmdbid']), target['season'])


def record(store, raw, candidate, source, target, receipt):
    """No network or tasks: save verified evidence as a review candidate."""
    code = candidate['share'].get('shareCode')
    if not code or candidate['share'].get('cloud') not in (None, '115'):
        raise ValueError('Only115 shares are supported')
    key = hashlib.sha256(f"{target['tmdbid']}:{target['season']}:{code}:{receipt['file_hash']}".encode()).hexdigest()[:20]
    prior = store.state_get('yun_candidates', key, {})
    value = {**prior, 'id': key, 'raw': asdict(raw), 'candidate': candidate, 'source_id': source['id'],
             'source_name': source.get('name') or source['kind'], 'target': target, 'parsed': receipt['parsed'],
             'files': (raw.extra.get('fnos_file_evidence') or {}).get('files', []),
             'pairs': (raw.extra.get('fnos_file_evidence') or {}).get('pairs', []),
             'file_hash': receipt['file_hash'], 'checked_at': time.time(),
             'state': prior.get('state') if prior.get('state') in ('ignored', 'selected', 'selecting') else 'available'}
    store.state_set('yun_candidates', key, value)
    return key


def public(store, item):
    parsed, target = item['parsed'], item['target']
    have = have_episodes(store, target)
    episodes = sorted({p[1] for p in item.get('pairs', []) if p[0] == target['season']})
    if not episodes:
        episodes = sorted(parsed.get('episodes') or [])
    sub = next((s for s in store.subscriptions() if s.get('tmdbid') == target['tmdbid'] and s['season'] == target['season']), {})
    release = parsed.get('release') or {}
    return {'id': item['id'], 'title': target['title'], 'year': parsed.get('year'), 'season': target['season'],
            'media_type': parsed.get('media_type'), 'original_title': (parsed.get('raw_fields') or {}).get('original_title'),
            'episodes': episodes, 'new_episodes': sorted(set(episodes) - have), 'overlap': sorted(set(episodes) & have),
            'missing': sorted(set(range(1, min(int(sub.get('total') or 0), 2000) + 1)) - have - set(episodes)),
            'resolution': parsed.get('resolution'), 'release': release, 'files': item.get('files', []),
            'bytes': sum(int(f.get('size') or 0) for f in item.get('files', [])), 'source': item['source_name'],
            'checked_at': item['checked_at'], 'reason': parsed.get('reason'), 'state': item['state'],
            'cloud': '115', 'share_url': 'https://115cdn.com/s/' + item['candidate']['share']['shareCode'],
            'password': item['candidate']['share'].get('password'), 'preferences_verified': item.get('quality') == 'ready',
            'task_id': item.get('task_id')}


def available(store, target=None):
    values = [v for v in store.state_items('yun_candidates').values() if v.get('state') == 'available'
              and time.time() - v['checked_at'] < 600 and (not target or v['target'] == target)]
    return sorted((v for v in values if public(store, v)['new_episodes']),
                  key=lambda v: (len(public(store, v)['new_episodes']), v['checked_at']), reverse=True)


def batch(store, target=None):
    items = available(store, target)[:3]
    identity = secrets.token_hex(8)
    value = {'id': identity, 'ids': [v['id'] for v in items], 'created_at': time.time()}
    store.state_set('yun_batches', identity, value)
    return {'id': identity, 'items': [public(store, v) for v in items]}


def next_batch(store, identity):
    value = store.state_get('yun_batches', identity)
    if not value or time.time() - value['created_at'] > 600:
        raise ValueError('This batch expired; refresh candidates')
    for key in value['ids']:
        item = store.state_get('yun_candidates', key, {})
        if item.get('state') == 'available':
            item['state'] = 'ignored'
            store.state_set('yun_candidates', key, item)
    return batch(store)


def ignore(store, identity):
    item = store.state_get('yun_candidates', identity)
    if not item or item['state'] != 'available':
        raise ValueError('Candidate is no longer available')
    item['state'] = 'ignored'
    store.state_set('yun_candidates', identity, item)


_locks = {}


async def select(service, identity):
    # Serialized within each server process; deterministic message ID also dedups across Bot/web.
    lock = _locks.setdefault(id(service.store), asyncio.Lock())
    async with lock:
        store = service.store
        item = store.state_get('yun_candidates', identity)
        if item and item.get('task_id'):
            return {'task_id': item['task_id'], 'state': 'already_selected'}
        if not item or item['state'] != 'available':
            raise ValueError('Candidate is no longer available')
        raw = RawMessage(**item['raw'])
        verdict, reason = await Preflight(service.pipeline).check(raw, item['candidate'], item['source_id'], item['target'])
        if verdict != 'accepted':
            item['state'] = 'unavailable' if verdict == 'excluded' else 'needs_recheck'
            store.state_set('yun_candidates', identity, item)
            raise ValueError('Candidate recheck did not pass: ' + reason)
        proof = store.state_get('fnos_preflight', raw.extra.get('fnos_preflight'), {})
        if item.get('file_hash') and proof.get('file_hash') and proof['file_hash'] != item['file_hash']:
            record(store, raw, item['candidate'], {'id': item['source_id'], 'name': item['source_name']}, item['target'], proof)
            item['state'] = 'changed'
            store.state_set('yun_candidates', identity, item)
            raise ValueError('Actual share files changed; review the refreshed candidate before selecting')
        # A concurrent next/ignore action may have happened while network verification ran.
        latest = store.state_get('yun_candidates', identity, {})
        if latest.get('state') != 'available':
            if latest.get('task_id'):
                return {'task_id': latest['task_id'], 'state': 'already_selected'}
            raise ValueError('Candidate was ignored or changed during recheck')
        if not public(store, item)['new_episodes']:
            raise ValueError('All these episodes are already stored')
        raw.extra['yun_resource_confirmed'] = True
        raw.msg_id = 'yun-choice-' + identity
        accepted = await service.pipeline.accept(PipelineContext(raw=raw))
        if accepted:
            item.update(state='selected', task_id=accepted.task_id)
            store.state_set('yun_candidates', identity, item)
            target = item['target']
            store.state_set('yun_follow', f"{target['tmdbid']}:{target['season']}", {'auto_missing': True, 'confirmed_at': time.time()})
            return {'task_id': accepted.task_id, 'state': 'received'}
        # Accept was already committed by another process: recover its task ID, never retransfer.
        row = store._conn.execute('SELECT task_id FROM tasks WHERE source=? AND msg_id=?', (raw.source, raw.msg_id)).fetchone()
        if row:
            item.update(state='selected', task_id=row['task_id'])
            store.state_set('yun_candidates', identity, item)
            return {'task_id': row['task_id'], 'state': 'already_selected'}
        raise ValueError('Selection could not be persisted')


def card(item):
    eps = ','.join(map(str, item['episodes']))
    release = item['release']
    detail = '\n'.join(f"{name}：{release.get(key) or '未知'}" for name, key in [
        ('字幕', 'subtitles'), ('字幕格式', 'subtitle_format'), ('音轨', 'audio'), ('HDR', 'hdr'),
        ('编码', 'codec'), ('片源', 'source'), ('发布组', 'group')])
    sizes = '\n'.join(f"{f['name']} · {int(f.get('size') or 0) / 1024**2:.1f} MiB" for f in item['files'][:12])
    return (f"{item['title']} ({item['year'] or '年份未知'}) 第{item['season']}季\n"
            f"实际{len(item['episodes'])}集：{eps}\n新增：{item['new_episodes']}；重叠：{item['overlap']}\n"
            f"选择后仍缺：{item['missing']}（按已登记总集数，播出状态另看订阅）\n"
            f"分辨率：{item['resolution'] or '未知'}\n{detail}\n总大小：{item['bytes']/1024**3:.2f} GiB\n"
            f"来源：{item['source']} · 115\n{item['share_url']}\n"
            f"依据：{item['reason'] or '已核验实际季集文件'}\n各集大小：\n{sizes}")[:3700]


def queue_batch_notice(store):
    items = available(store)
    if not items:
        return
    # Only one pending batch at a time; new scans cannot spam another three cards.
    pending = store.state_get('yun_batches', 'notified', {})
    if pending:
        old = store.state_get('yun_batches', pending.get('id', ''), {})
        if any(store.state_get('yun_candidates', key, {}).get('state') == 'available' for key in old.get('ids', [])):
            return
    value = batch(store)
    store.state_set('yun_bot', 'batch', value)
    for item in value['items']:
        store.stage_notice('yun-candidate-' + item['id'], card(item), ['fnos_bot'], buttons=[[
            {'text': '选择此资源', 'callback_data': 'c:select:' + item['id']},
            {'text': '忽略', 'callback_data': 'c:ignore:' + item['id']}],
            [{'text': '文件详情', 'callback_data': 'c:detail:' + item['id']}],
            [{'text': '下一批（忽略本批3个）', 'callback_data': 'b:next:' + value['id']}]] )
    store.state_set('yun_batches', 'notified', {'created_at': time.time(), 'id': value['id']})
