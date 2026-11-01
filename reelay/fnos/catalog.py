"""Owner-managed source registry. Credentials are never part of source records."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit


def normalize(kind: str, url: str) -> str:
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError('Source URL is too long')
    parsed = urlsplit(url.strip())
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Do not include credentials or fragments in source URLs')
    if kind == 'telegram':
        if parsed.scheme != 'https' or parsed.netloc.lower() != 't.me':
            raise ValueError('Use a public https://t.me/channel link')
        name = parsed.path.removeprefix('/s/').strip('/')
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', name) or parsed.query:
            raise ValueError('Only public channel usernames are supported')
        return 'https://t.me/' + name.lower()
    if kind == 'kdocs':
        if parsed.scheme != 'https' or parsed.netloc.lower() not in ('kdocs.cn', 'www.kdocs.cn', '365.kdocs.cn') or not re.fullmatch(r'/l/[A-Za-z0-9]+', parsed.path):
            raise ValueError('Use a KDocs HTTPS /l/ share link')
        return 'https://www.kdocs.cn' + parsed.path
    if kind == 'pansou':
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.path not in ('', '/'):
            raise ValueError('Use the PanSou service base URL without a path or credentials')
        return parsed.scheme + '://' + parsed.netloc.lower()
    raise ValueError('Unknown source kind')


def infer(url: str) -> str:
    host = urlsplit(url).hostname
    if host == 't.me':
        return 'telegram'
    if host in ('kdocs.cn', 'www.kdocs.cn', '365.kdocs.cn'):
        return 'kdocs'
    raise ValueError('For PanSou use /source add pansou http://host:port')


class Catalog:
    def __init__(self, store):
        self.store = store

    def _change(self, action):
        with self.store._lock, self.store._conn:
            self.store._conn.execute('BEGIN IMMEDIATE')
            row = self.store._conn.execute("SELECT value FROM plugin_state WHERE plugin='fnos_catalog' AND key='sources'").fetchone()
            items = json.loads(row['value']) if row else []
            result = action(items)
            self.store._conn.execute("INSERT INTO plugin_state VALUES ('fnos_catalog','sources',?,?) ON CONFLICT(plugin,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (json.dumps(items), time.time()))
        self.wake()
        return result

    def seed(self, cfg, snapshot=None):
        if self.store.state_get('fnos_catalog', 'sources') is not None:
            return
        defaults = cfg.get('plugins', {}).get('fnos_sources', {})
        for channel in defaults.get('channels', []):
            self.add('telegram', 'https://t.me/' + channel)
        if defaults.get('pansou_url'):
            self.add('pansou', defaults['pansou_url'])
        document = (snapshot or {}).get('source', {}).get('url') or defaults.get('kdocs_url')
        if document:
            item = self.add('kdocs', document)
            if snapshot:
                self.store.state_set('fnos_documents', item['id'], snapshot)

    def add(self, kind, url, name='', note='', interval_seconds=600):
        url = normalize(kind, url)
        identity = hashlib.sha256((kind + ':' + url).encode()).hexdigest()[:16]
        def change(items):
            existing = next((s for s in items if s['id'] == identity), None)
            if existing:
                existing['enabled'] = True
                return dict(existing)
            if len(items) >= 20:
                raise ValueError('At most20 sources are supported')
            value = {'id': identity, 'kind': kind, 'url': url, 'enabled': True, 'created_at': time.time(),
                     'name': str(name or kind)[:80], 'note': str(note)[:500], 'interval_seconds': max(60, min(86400, int(interval_seconds)))}
            items.append(value)
            return value
        return self._change(change)

    def update(self, identity, values):
        if set(values) - {'name', 'note', 'enabled', 'interval_seconds'}:
            raise ValueError('Unsupported source field')
        if 'enabled' in values and type(values['enabled']) is not bool:
            raise ValueError('enabled must be boolean')
        if 'interval_seconds' in values and (type(values['interval_seconds']) is not int or not 60 <= values['interval_seconds'] <= 86400):
            raise ValueError('Scan interval must be60–86400 seconds')
        if any(k in values and (not isinstance(values[k], str) or len(values[k]) > n) for k, n in [('name', 80), ('note', 500)]):
            raise ValueError('Source name or note too long')
        def change(items):
            source = next((s for s in items if s['id'] == identity), None)
            if not source:
                raise ValueError('Source not found')
            source.update(values)
            return dict(source)
        return self._change(change)

    def remove(self, identity):
        def change(items):
            if not any(s['id'] == identity for s in items):
                raise ValueError('Source not found')
            items[:] = [s for s in items if s['id'] != identity]
        self._change(change)

    def wake(self):
        self.store.state_set('fnos_catalog', 'wake', time.time_ns())

    def items(self):
        return self.store.state_get('fnos_catalog', 'sources', [])

    def health(self, identity, state, **fields):
        data = self.store.state_get('fnos_source_health', identity, {})
        data.update(state=state, checked_at=time.time(), **fields)
        if state == 'ok':
            data.pop('error', None)
            data['last_success_at'] = time.time()
        self.store.state_set('fnos_source_health', identity, data)

    def public(self):
        result = []
        for source in self.items():
            health = self.store.state_get('fnos_source_health', source['id'], {'state': 'not_checked'})
            if source['kind'] == 'kdocs':
                snapshot = self.store.state_get('fnos_documents', source['id'], {})
                observed = snapshot.get('received_at', 0)
                failed_sync = health.get('sync_state') not in (None, 'ok') and health.get('last_sync_attempt_at', 0) >= observed
                health = {**health, 'sync_mode': health.get('sync_mode', 'authorized_cli_bridge'), 'last_sync_at': observed,
                          'completeness': snapshot.get('completeness', {}).get('status', 'unknown'),
                          'state': health['sync_state'] if failed_sync else ('ok' if observed and time.time() - observed < 1800 else ('stale' if observed else 'needs_sync'))}
            result.append({**source, 'health': health})
        return result

    def snapshot(self, identity, snapshot):
        if not isinstance(snapshot, dict):
            raise ValueError('Snapshot must be an object')
        source = next((s for s in self.items() if s['id'] == identity and s['kind'] == 'kdocs'), None)
        if not source or normalize('kdocs', snapshot.get('source', {}).get('url', '')) != source['url']:
            raise ValueError('Snapshot must belong to a registered document')
        if snapshot.get('status') != 'ok' or snapshot.get('contentPersisted') is not False:
            raise ValueError('Only successful candidate-only snapshots are accepted')
        candidates = snapshot.get('candidates', {}).get('shares115', [])
        if not isinstance(candidates, list) or len(candidates) > 5000:
            raise ValueError('Too many candidates')
        clean = []
        for share in candidates:
            code, password = share.get('shareCode'), share.get('password')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9]{1,100}', code):
                raise ValueError('Invalid115 share code')
            if password and (not isinstance(password, str) or not re.fullmatch(r'[A-Za-z0-9]{4}', password)):
                raise ValueError('Invalid115 extraction code')
            clean.append({'shareCode': code, 'password': password, 'contextSnippet': str(share.get('contextSnippet') or '')[:160]})
        completeness = snapshot.get('completeness') or {}
        clean_completeness = {'status': str(completeness.get('status', 'uncertain'))[:40],
                              'spreadsheet': completeness.get('spreadsheet') is True,
                              'warnings': [str(x)[:160] for x in (completeness.get('warnings') or [])[:10]]}
        value = {'source': {'url': source['url']}, 'status': 'ok', 'contentPersisted': False,
                 'contentHash': str(snapshot.get('contentHash') or '')[:64], 'received_at': time.time(),
                 'completeness': clean_completeness, 'candidates': {'shares115': clean}}
        from .offline import identity as offline_identity
        offline = []
        for item in (snapshot.get('candidates', {}).get('offlineLinks') or [])[:100]:
            try:
                uri = str(item['uri'])
                offline_identity(uri)
                offline.append({'uri': uri, 'contextSnippet': str(item.get('contextSnippet') or '')[:160]})
            except (ValueError, KeyError, TypeError):
                continue
        value['candidates']['offlineLinks'] = offline
        self.store.state_set('fnos_documents', identity, value)
        self.health(identity, 'ok', count=len(clean), sync_state='ok', last_sync_attempt_at=time.time())
        self.wake()
        return {'source_id': identity, 'candidates': len(clean), 'received_at': value['received_at']}

    def sync_status(self, identity, state):
        if state not in ('auth_error', 'permission_denied', 'rate_limited', 'sync_error'):
            raise ValueError('Invalid synchronization state')
        if not any(s['id'] == identity and s['kind'] == 'kdocs' for s in self.items()):
            raise ValueError('Document source not found')
        self.health(identity, state, sync_state=state, last_sync_attempt_at=time.time())
