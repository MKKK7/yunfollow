"""Read-only checks of existing services. Never expose their credentials."""
import json
import asyncio
import os
import time

from ..core import paths
from .http import api
from .media import LitePan
from .catalog import Catalog


def latest(store):
    result = {'readiness': store.state_get('fnos_integrations', 'last', {}),
              'last_isolated_acceptance': None}
    proof = paths.DATA / 'fnos-live-acceptance.json'
    if proof.exists() and proof.stat().st_size < 65536:
        value = json.loads(proof.read_text(encoding='utf-8'))
        result['last_isolated_acceptance'] = {k: value.get(k) for k in (
            'status', 'task_id', 'metadata_files', 'metadata_bytes', 'pipeline', 'note', 'stages')}
    return result


async def check(service):
    result = {'checked_at': time.time(), 'services': {}, 'read_only': True}
    lp = LitePan()
    try:
        await lp.login()
        cfg = service.pipeline.cfg.get('plugins', {}).get('fnos_media', {})
        tasks = await lp.call('/api/admin/strm/tasks')
        task_id = int(cfg.get('strm_task_id', 0))
        task = next((t for t in tasks if t['id'] == task_id), None)
        settings = await lp.call('/api/admin/strm/settings')
        result['services']['LitePan'] = {'ready': task is not None, 'strm_task_id': task_id,
            'metadata_sync_mode': settings.get('metadata_sync_mode')}
        emby = await lp.call('/api/admin/emby/configs')
        available = any(x.get('id') == cfg.get('emby_config_id') and x.get('api_key') for x in emby.get('items', []))
        libraries = await lp.call('/api/admin/emby/libraries?config_id=' + str(cfg.get('emby_config_id') or ''))
        available = available and bool(libraries)
        result['services']['Emby_via_LitePan'] = {'ready': bool(emby.get('enabled') and available),
                                                'new_emby_key_required': False}
    except Exception as exc:
        result['services']['LitePan'] = {'ready': False, 'error': type(exc).__name__}
        result['services']['Emby_via_LitePan'] = {'ready': False, 'error': 'LitePan connection not verified'}
    try:
        base = os.getenv('OPENLIST_URL', '').rstrip('/')
        mount = os.getenv('OPENLIST_MOUNT', '/115网盘').rstrip('/')
        response = await api(base + '/api/fs/list', {'path': mount, 'page': 1, 'per_page': 1, 'refresh': False},
                             headers={'Authorization': os.getenv('OPENLIST_TOKEN', '')})
        result['services']['OpenList_metadata'] = {'ready': response.get('code') == 200, 'video_upload_allowed': False}
    except Exception as exc:
        result['services']['OpenList_metadata'] = {'ready': False, 'error': type(exc).__name__}
    storage = service.pipeline.reg.get_provider('storage', 'p115')
    configured = storage.ready()
    try:
        cid = service.pipeline.cfg['save_dirs'][service.pipeline.cfg['defaults']['save_to']]
        if not configured or str(cid) == '0':
            raise ValueError('Library root not configured')
        await asyncio.to_thread(storage.browse, str(cid), 0, 1)
        result['services']['115'] = {'configured': configured, 'ready': True, 'mode': 'native_cloud'}
    except Exception as exc:
        result['services']['115'] = {'configured': configured, 'ready': False, 'error': type(exc).__name__}
    sources = Catalog(service.store).public()
    for source in sources:
        if not source.get('enabled'):
            continue
        if source['kind'] == 'pansou':
            name = 'PanSou · ' + (source.get('name') or '盘搜')
            try:
                response = await api(source['url'] + '/api/health', timeout=15)
                result['services'][name] = {'ready': bool(response) and response.get('code') in (None, 0, 200)}
            except Exception as exc:
                result['services'][name] = {'ready': False, 'error': type(exc).__name__}
        else:
            name = source.get('name') or source['kind']
            health = source['health']
            result['services'][name] = {'ready': health.get('state') == 'ok' and time.time() - health.get('last_success_at', 0) < 1800,
                                        'last_read_at': health.get('last_success_at'), 'source_check': True}
    for component, label in [('llm', '模型接口'), ('tmdb', 'TMDB')]:
        observed = service.store.health_status().get(component, {})
        result['services'][label] = {'ready': observed.get('status') == 'connected' and not observed.get('stale'),
            'error': observed.get('error') or ('最近没有成功调用记录' if observed.get('stale', True) else '')}
    service.store.state_set('fnos_integrations', 'last', result)
    return result
