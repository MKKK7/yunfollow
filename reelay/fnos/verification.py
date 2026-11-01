"""Read-only final checks. Refresh accepted is distinct from visible Emby episodes."""
import asyncio
import os
from urllib.parse import urlencode

from .http import api
from .media import LitePan
from .timeline import stage


def existing_connection(snapshot):
    """Never treat LitePan's masked key as a usable credential."""
    key = os.getenv('YUNFOLLOW_EMBY_READ_KEY', '')
    if key:
        return {**snapshot, 'api_key': key}
    if '*' not in snapshot.get('api_key', ''):
        return snapshot
    return None


async def verify(ctx, service):
    stage(ctx, 'verify', 'running', provider='115 / Emby')
    storage = service.pipeline.reg.get_provider('storage', ctx.ns('storage').get('provider', 'p115'))
    drive = storage.library()
    plan = ctx.ns('organize').get('plan', [])
    if not plan:
        raise ValueError('No organized file evidence')
    for file in plan:
        attr = await asyncio.to_thread(drive.get_attr, int(file['src_id']))
        if attr['name'] != file['target_name'] or int(attr.get('size') or 0) != int(file.get('size') or 0):
            raise ValueError('Cloud video file no longer matches verified plan')
    lp = LitePan()
    await lp.login()
    settings = service.pipeline.cfg.get('plugins', {}).get('fnos_media', {})
    configs = await lp.call('/api/admin/emby/configs')
    config = next((c for c in configs.get('items', []) if c['id'] == settings.get('emby_config_id')), None)
    if not config:
        raise ValueError('Existing LitePan Emby connection missing')
    config = await asyncio.to_thread(existing_connection, config)
    if config is None:
        receipt = {'provider': '115 / Emby', 'cloud_files_verified': len(plan), 'emby_indexed': None,
                   'reason': 'LitePan仅提供刷新接口；媒体条目核验等待专用API权限，未将刷新接受冒充入库成功'}
        stage(ctx, 'verify', 'waiting_confirmation', **receipt)
        return receipt
    url = config['emby_url'].rstrip('/')
    headers = {'X-Emby-Token': config['api_key']}
    tmdbid = str(ctx.meta.get('tmdb', {}).get('tmdbid') or '')
    params = {'Recursive': 'true', 'IncludeItemTypes': 'Series', 'AnyProviderIdEquals': 'tmdb.' + tmdbid,
              'Fields': 'ProviderIds', 'Limit': 20}
    r = await api(url + '/Items?' + urlencode(params), headers=headers, timeout=15)
    show = next((x for x in r.get('Items', []) if str((x.get('ProviderIds') or {}).get('Tmdb')) == tmdbid), None)
    visible = []
    if show:
        r = await api(url + '/Shows/' + show['Id'] + '/Episodes?' + urlencode({'Season': ctx.parsed.season, 'Fields': 'Path', 'Limit': 2000}), headers=headers, timeout=15)
        visible = sorted({int(x['IndexNumber']) for x in r.get('Items', []) if x.get('ParentIndexNumber') == ctx.parsed.season and x.get('IndexNumber')})
    wanted = set(ctx.parsed.episodes)
    indexed = wanted.issubset(set(visible))
    receipt = {'provider': '115 / Emby', 'cloud_files_verified': len(plan), 'emby_indexed': indexed,
               'emby_visible_episodes': visible, 'emby_pending_episodes': sorted(wanted - set(visible))}
    stage(ctx, 'verify', 'done' if indexed else 'waiting_retry', **receipt)
    return receipt
