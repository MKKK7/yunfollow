"""Read-only checks of existing services. Never expose their credentials."""
import json
import asyncio
import os
import time

from ..core import paths, ai, tmdb, secret_store
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
    try:
        with service.pipeline.scope():
            response = await asyncio.to_thread(tmdb._request, '/configuration', {}, timeout=10)
        result['services']['TMDB'] = {'ready': bool(response.get('images'))}
    except Exception as exc:
        result['services']['TMDB'] = {'ready': False, 'configured': bool(secret_store.get('tmdb_api_key')), 'error': type(exc).__name__}
    try:
        client, model = service.ai_client()
        def valid(value):
            if not isinstance(value, dict) or value != {'ok': True}:
                raise ValueError('Invalid health response')
            return value
        with service.pipeline.scope():
            await ai.complete_json(client, model=model, purpose='yunfollow_connection_check',
                messages=[{'role': 'user', 'content': 'Only output {"ok":true}.'}],
                schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False},
                name='connection_check', validate=valid, rule=service.pipeline.cfg.get('llm', {}), timeout=30, invalid_retries=0)
        result['services']['模型接口'] = {'ready': True}
    except Exception as exc:
        result['services']['模型接口'] = {'ready': False, 'configured': bool(secret_store.get('llm_api_key')), 'error': type(exc).__name__}
    service.store.state_set('fnos_integrations', 'last', result)
    return result
