"""Configured collectors -> read-only preflight -> qualified pipeline tasks."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path

from ..core import paths
from ..core.config import load_config
from ..core.contracts import RawMessage
from ..core.parse_rules import parse_115_url
from ..core.registry import register
from .catalog import Catalog
from .http import api
from .preflight import Preflight
from .discovery import discover, describe

log = logging.getLogger('reelay.fnos.search')


def search_queries(sub):
    return [sub['title'], sub['title'] + ' 剧集', sub['title'] + ' 动画']


def matching(candidate, subscriptions):
    title = str(candidate.get('title') or '').casefold()
    tmdb = str(candidate.get('tmdbid') or '')
    return [s for s in subscriptions if s.get('auto') and not s.get('finished') and (
        bool(tmdb and str(s.get('tmdbid')) == tmdb)
        or bool(s.get('title') and str(s['title']).casefold() in title))]


async def command_json(command):
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), 180)
        if proc.returncode not in (0, 2) or len(output) > 1024 * 1024:
            raise RuntimeError('Collector failed')
        return json.loads(output)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@register(kind='provider', ptype='source')
class FnosSources:
    name = 'fnos_sources'
    backfill_done = True

    def __init__(self):
        self._closed = False
        self._status, self._error = 'waiting', ''

    def configure(self, targets):
        self.targets = targets

    def bind_pipeline(self, pipeline):
        self.pipeline = pipeline

    def health(self):
        return {'status': self._status, 'error': self._error}

    async def close(self):
        self._closed = True

    async def collect(self, source, subs, root, catalog, update):
        """Returns a current snapshot; failures never masquerade as empty success."""
        items = {}
        if source['kind'] == 'kdocs':
            value = catalog.store.state_get('fnos_documents', source['id'], {})
            if time.time() - value.get('received_at', 0) > 1800:
                raise RuntimeError('Document synchronization is stale or not authorized')
            for share in value.get('candidates', {}).get('shares115', []):
                items[share['shareCode']] = {'title': share.get('contextSnippet', ''), 'share': share,
                                            'version': value.get('contentHash', ''), 'source': '@fnos_document'}
        elif source['kind'] == 'pansou':
            for sub in subs[:10]:
                for query in search_queries(sub):
                    update('正在自动盘搜：' + query, query=query)
                    result = await api(source['url'] + '/api/search',
                        {'kw': query, 'res': 'merge', 'src': 'all'}, timeout=45)
                    if result.get('code') not in (None, 0, 200):
                        raise RuntimeError('PanSou rejected search')
                    hits = (result.get('data') or {}).get('merged_by_type', {}).get('115', []) or []
                    update(f'盘搜「{query}」返回{len(hits)}条未核验候选')
                    for share in hits[:40]:
                        _, code, password = parse_115_url(share.get('url', ''), [share.get('url', '')])
                        if not code:
                            continue
                        title = str(share.get('title') or share.get('note') or '')[:2000]
                        new = {'title': title, 'share': {'shareCode': code, 'password': share.get('password') or password},
                               'source': '@fnos_pansou', 'version': hashlib.sha256(json.dumps(share, sort_keys=True).encode()).hexdigest()}
                        if code in items and items[code]['share'].get('password') != new['share'].get('password'):
                            new['conflict'] = True
                        items[code] = new
                    merged = (result.get('data') or {}).get('merged_by_type', {})
                    for cloud in ('aliyun', 'quark'):
                        for share in sorted(merged.get(cloud, []), key=lambda s: str(s.get('datetime') or ''), reverse=True)[:40]:
                            title = str(share.get('title') or share.get('note') or '')[:2000]
                            if any(word in title for word in ('大电影', '电影版', '剧场版')):
                                continue
                            url = str(share.get('url') or '')
                            items[cloud + url] = {'title': title, 'datetime': str(share.get('datetime') or ''), 'share': {'cloud': cloud, 'url': url,
                                'password': share.get('password') or ''}, 'source': '@fnos_pansou',
                                'version': hashlib.sha256(json.dumps(share, sort_keys=True).encode()).hexdigest()}
        else:
            channel = source['url'].rsplit('/', 1)[-1]
            for query in [s['title'] for s in subs[:5]]:
                update('正在搜索 Telegram 频道：' + channel + ' / ' + query, query=query)
                result = await command_json(['node', '/app/fnos-adapters/telegram.mjs', '--channel', channel,
                    '--search', query, '--limit', '5', '--snapshot-dir', str(root / source['id'] / hashlib.sha256(query.encode()).hexdigest()[:12])])
                if result.get('errors'):
                    raise RuntimeError('Telegram public archive was only partially readable')
                events = json.loads(Path(result['output']).read_text(encoding='utf-8'))
                for event in events.get('observations', events.get('events', [])):
                    for article in event.get('articles', []):
                        for share in article.get('shares', []):
                            items[share['shareCode']] = {'title': article.get('title') or event.get('title', ''),
                                'evidence': str(article.get('evidence') or event.get('text') or '')[:6000],
                                'tmdbid': article.get('tmdbId'), 'media_type': article.get('mediaType'),
                                'share': share, 'source': '@fnos_public', 'version': article['articleHash']}
        return items

    async def scan(self, emit):
        pipeline, store = self.pipeline, self.pipeline.store
        pipeline.reload_config()
        cfg = pipeline.cfg
        catalog = Catalog(store)
        snapshot_file = paths.DATA / 'fnos-document-candidates.json'
        snapshot = json.loads(snapshot_file.read_text(encoding='utf-8')) if snapshot_file.exists() and snapshot_file.stat().st_size < 8 * 1024 * 1024 else {}
        catalog.seed(cfg, snapshot)
        subs = [s for s in store.subscriptions() if s.get('auto') and not s.get('finished')]
        progress = {'round_id': str(time.time_ns()), 'state': 'searching', 'started_at': time.time(),
                    'collected': 0, 'matched': 0, 'checked': 0, 'emitted': 0, 'excluded': 0, 'deferred': 0,
                    'queries': [], 'errors': []}
        def update(message, query=None, **fields):
            if query:
                progress['queries'].append(query)
            progress.update(message=message, updated_at=time.time(), **fields)
            store.state_set(self.name, 'progress', progress)
            log.info('%s', message)
        if not subs:
            update('等待确认订阅；未执行资源转存', state='waiting')
            return
        update('开始自动搜索已确认剧集：' + '、'.join(s['title'] for s in subs))
        root = paths.DATA / 'fnos-sources'
        root.mkdir(parents=True, exist_ok=True)
        preflight = Preflight(pipeline)
        seen = set()
        sources = sorted((s for s in catalog.items() if s['enabled']), key=lambda s: {'pansou': 0, 'kdocs': 1, 'telegram': 2}[s['kind']])
        for source in sources:
            try:
                candidates = await self.collect(source, subs, root, catalog, update)
                progress['collected'] += len(candidates)
                catalog.health(source['id'], 'ok', count=len(candidates))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = 'stale' if source['kind'] == 'kdocs' else 'error'
                catalog.health(source['id'], code, error=type(exc).__name__)
                progress['errors'].append({'source_id': source['id'], 'kind': source['kind'], 'error': code})
                update('来源未完成读取：' + source['kind'] + '，继续其他来源')
                continue
            values = sorted(candidates.values(), key=lambda c: (not bool(c['share'].get('cloud')), c.get('datetime', '')), reverse=True)
            cursor = store.state_get('fnos_source_cursor', source['id'], 0) % max(1, len(values))
            ordered = [(i, values[i]) for i in [*range(cursor, len(values)), *range(cursor)]]
            source_checks = 0
            for index, candidate in ordered:
                for sub in matching(candidate, subs):
                    share = candidate['share']
                    key = (share.get('shareCode') or share.get('url'), share.get('password'), sub['id'])
                    if key in seen:
                        continue
                    seen.add(key)
                    progress['matched'] += 1
                    if progress['checked'] >= 30 or source_checks >= max(1, 30 // max(1, len(sources))):
                        progress['deferred'] += 1
                        continue
                    source_checks += 1
                    store.state_set('fnos_source_cursor', source['id'], (index + 1) % max(1, len(values)))
                    if share.get('cloud'):
                        progress['checked'] += 1
                        update('正在读取其他网盘的实际文件列表：' + candidate['title'][:80])
                        target = {'title': sub['title'], 'tmdbid': sub['tmdbid'], 'season': sub['season']}
                        found = await discover(store, candidate, target)
                        if found['state'] == 'verified_files':
                            progress['external_verified'] = progress.get('external_verified', 0) + 1
                            update(describe(found))
                            notice = found['id'] + ':' + str(max(found['episodes']))
                            if not store.state_get('fnos_discovery_notices', notice):
                                store.stage_notice('discovery-' + notice, describe(found), ['fnos_bot'])
                                store.state_set('fnos_discovery_notices', notice, True)
                        else:
                            progress['deferred'] += 1
                        continue
                    link = 'https://115cdn.com/s/' + share['shareCode']
                    if share.get('password'):
                        link += '?password=' + share['password']
                    target = {'title': sub['title'], 'tmdbid': sub['tmdbid'], 'season': sub['season'], 'media_type': 'tv'}
                    text = candidate['title'][:500] + '\n' + candidate.get('evidence', '')[:6000] + '\n' + link
                    digest = hashlib.sha256(('fnos-v3:' + str(sub['id']) + text + candidate['version']).encode()).hexdigest()[:24]
                    raw = RawMessage(source=candidate['source'], msg_id=digest, text=text, urls=[link], extra={'fnos_subscription': target})
                    progress['checked'] += 1
                    update(f"入队前核验：{sub['title']}，候选{progress['checked']}")
                    if candidate.get('conflict'):
                        verdict, reason = 'deferred', 'extraction_code_conflict'
                    else:
                        verdict, reason = await preflight.check(raw, candidate, source['id'], target)
                    if verdict != 'accepted':
                        progress['excluded' if verdict == 'excluded' else 'deferred'] += 1
                        update(f'候选未入队：{reason}')
                        continue
                    receipt = store.state_get('fnos_preflight', raw.extra['fnos_preflight'], {})
                    raw.msg_id = hashlib.sha256((raw.msg_id + receipt.get('file_hash', '')).encode()).hexdigest()[:24]
                    accepted = await emit(raw)
                    if accepted:
                        progress['emitted'] += 1
                        update(f'合格剧集候选已入队，任务{accepted.task_id}')
        self._status = 'degraded' if progress['errors'] else 'connected'
        self._error = '部分来源未读取成功' if progress['errors'] else ''
        message = (f"自动搜索完成：核验{progress['checked']}，排除{progress['excluded']}，"
                   f"暂无法核验{progress['deferred']}，新增任务{progress['emitted']}。")
        if not progress['emitted']:
            message += '本轮没有新增合格任务；未发起转存。'
        if progress.get('external_verified'):
            message += f"已找到{progress['external_verified']}个其他网盘的实际剧集文件来源；/results 查看链接，未下载中转。"
        update(message, state='waiting', finished_at=time.time())
        fingerprint = hashlib.sha256(json.dumps(subs, sort_keys=True).encode()).hexdigest()[:16]
        if store.state_get(self.name, 'reported_subscriptions') != fingerprint:
            store.stage_notice('fnos-preflight-' + fingerprint, message + '\n发送 /status 查看真实任务状态，/audit 查看排除原因。', ['fnos_bot'])
            store.state_set(self.name, 'reported_subscriptions', fingerprint)

    async def listen(self, emit):
        while not self._closed:
            catalog = Catalog(self.pipeline.store)
            before = (catalog.store.state_get('fnos_catalog', 'wake'), json.dumps(catalog.store.subscriptions(), sort_keys=True))
            await self.scan(emit)
            interval = max(60, int(load_config().get('plugins', {}).get(self.name, {}).get('poll_seconds', 600)))
            until = time.monotonic() + interval
            while not self._closed and time.monotonic() < until:
                await asyncio.sleep(2)
                now = (catalog.store.state_get('fnos_catalog', 'wake'), json.dumps(catalog.store.subscriptions(), sort_keys=True))
                if now != before:
                    break
