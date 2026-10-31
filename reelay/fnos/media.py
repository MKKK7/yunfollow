"""Thin orchestration of existing LitePan services; no video HTTP/download API."""
import asyncio
import http.cookiejar
import json
import hashlib
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from ..core import runtime
from ..core.registry import register
from .http import api, IntegrationError
from .timeline import stage


class LitePan:
    def __init__(self):
        self.base = os.getenv('LITEPAN_URL', 'http://127.0.0.1:5211').rstrip('/')
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def _call(self, path, data=None, *, login=False):
        if login:
            body = urllib.parse.urlencode({'username': os.getenv('LITEPAN_USERNAME', 'admin'),
                                          'password': os.getenv('LITEPAN_PASSWORD', ''), 'remember': '0'}).encode()
            content = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data).encode() if data is not None else None
            content = 'application/json'
        req = urllib.request.Request(self.base + path, data=body, headers={'Content-Type': content})
        try:
            with self.opener.open(req, timeout=60) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise IntegrationError('LitePan response too large')
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise IntegrationError(f'LitePan HTTP {exc.code}; inspect the corresponding LitePan request log') from None
        except Exception:
            raise IntegrationError('LitePan request failed; check configuration and service health') from None
        if not result.get('success'):
            raise IntegrationError('LitePan operation was not successful')
        return result.get('data')

    async def login(self):
        await asyncio.to_thread(self._call, '/api/auth/login', login=True)

    async def call(self, path, data=None):
        return await asyncio.to_thread(self._call, path, data)


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value or '\x00' in value:
        raise ValueError('Unsafe relative path')
    return path


def metadata_files(root):
    """Only NFO and artwork, bounded even if a library contains unexpected files."""
    root = Path(root).resolve()
    result, total = [], 0
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('.nfo', '.jpg', '.jpeg', '.png', '.webp'):
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('Metadata escapes root')
        size = path.stat().st_size
        total += size
        if size > 10 * 1024 * 1024 or total > 100 * 1024 * 1024 or len(result) >= 100:
            raise ValueError('Metadata exceeds transfer budget')
        result.append(path)
    return result


@register(kind='hook', stage='after_save', order=40)
class FnosMedia:
    name = 'fnos_media'
    default_enabled = False
    stage, order = 'after_save', 40

    async def run(self, ctx):
        try:
            return await self._run(ctx)
        except Exception:
            for name, record in list(ctx.ns('fnos_workflow').get('stages', {}).items()):
                if record.get('state') == 'running':
                    stage(ctx, name, 'failed', **record.get('receipt', {}))
            raise

    async def _run(self, ctx):
        if not ctx.ns('organize').get('applied') or ctx.ns('organize').get('error'):
            raise ValueError('Cloud organization must be verified before media integration')
        state = ctx.ns(self.name)
        if state.get('complete'):
            return True
        stage(ctx, 'transfer', 'done', provider='115', verified=True)
        stage(ctx, 'organize', 'done', provider='115', verified=True)
        stage(ctx, 'strm', 'running', provider='LitePan')
        lp = LitePan()
        await lp.login()
        settings = runtime.get('plugins.fnos_media', {})
        task_id = int(settings.get('strm_task_id', 2))
        task = next((t for t in await lp.call('/api/admin/strm/tasks') if t['id'] == task_id), None)
        if not task:
            raise ValueError('Configured LitePan STRM task is missing')
        storage = runtime.services().storage(ctx.ns('storage').get('provider', 'p115'))
        drive = storage.library()
        groups = {}
        for item in ctx.ns('organize').get('plan', []):
            attr = await asyncio.to_thread(drive.get_attr, int(item['src_id']))
            if attr['name'] != item['target_name']:
                raise ValueError('Organized file changed; manual review required')
            parent = str(attr['parent_id'])
            groups.setdefault(parent, []).append(attr)
        if not groups:
            raise ValueError('No verified cloud files to process')
        works = set()
        generation_receipts = []
        for parent in groups:
            cloud = await asyncio.to_thread(storage.dir_path, parent)
            relative = PurePosixPath(cloud).relative_to(PurePosixPath(task['path']))
            safe_relative(str(relative))
            items = await asyncio.to_thread(drive.iterdir, int(parent))
            payload = {'account_id': task['account_id'], 'parent_id': parent, 'path': cloud,
                       'items': [{'id': str(a['id']), 'name': a['name'], 'size': a.get('size', 0), 'is_dir': a.get('is_dir', False)} for a in items]}
            status = await lp.call('/api/admin/strm/directory-status', payload)
            if status.get('matched_task_id') != task_id:
                raise ValueError('Directory matched a different LitePan task; no changes made')
            generated = await lp.call('/api/admin/strm/generate-current-directory', payload)
            if generated.get('skipped_conflict') or generated.get('deleted'):
                raise ValueError('Unexpected LitePan generation conflict/deletion')
            generation_receipts.append({k: generated.get(k) for k in ('matched_task_id', 'created', 'updated', 'skipped_existing', 'media_count')})
            # TV scrape index groups by show folder, one level above Season N.
            rel = relative.parent if ctx.parsed.media_type in ('tv', 'anime') or ctx.parsed.episodes else relative
            works.add(str(rel))
        stage(ctx, 'strm', 'done', provider='LitePan', task_id=task_id, results=generation_receipts)
        stage(ctx, 'scrape', 'running', provider='LitePan')
        progress = await lp.call('/api/admin/strm-scrape/progress')
        if progress.get('running'):
            raise ValueError('LitePan scraper is busy; retry integration later')
        index = await lp.call('/api/admin/strm-scrape/refresh-index', {'strm_task_id': task_id, 'limit': 200, 'keyword': ctx.parsed.title})
        tmdbid = str(ctx.meta.get('tmdb', {}).get('tmdbid') or ctx.parsed.raw_fields.get('tmdbid') or '')
        if not tmdbid.isdigit():
            raise ValueError('Verified TMDB ID required')
        for rel in works:
            item = next((i for i in index.get('items', []) if i.get('rel_dir', '').strip('/') == rel.strip('/')), None)
            if not item:
                raise ValueError('LitePan work index did not match exact organized directory')
            await lp.call('/api/admin/strm-scrape/rematch', {'strm_task_id': task_id, 'item_id': item['id'],
                          'tmdb_id': tmdbid, 'media_type': 'tv' if ctx.parsed.episodes or ctx.parsed.media_type in ('tv', 'anime') else 'movie'})
            for _ in range(120):
                await asyncio.sleep(2)
                progress = await lp.call('/api/admin/strm-scrape/progress')
                if not progress.get('running'):
                    break
            else:
                raise ValueError('LitePan scrape timeout; inspect existing task before retry')
            if progress.get('failed') or progress.get('error') or not progress.get('done'):
                raise ValueError('LitePan scrape did not complete successfully')
            stage(ctx, 'scrape', 'done', provider='LitePan', task_id=task_id, item_id=item['id'], done=progress['done'], failed=progress.get('failed', 0))
            # Keep LitePan's global sync policy unchanged. Reuse the already deployed
            # OpenList metadata PUT API when its mode does not upload local metadata.
            output = '/'.join(x.strip('/') for x in (task.get('group_dir', ''), task.get('output_folder', '')) if x)
            local = Path(os.getenv('FNOS_STRM_ROOT', '/app/strm')) / safe_relative(output) / safe_relative(rel)
            stage(ctx, 'writeback', 'running', provider='OpenList')
            receipt = await self.writeback(local, str(PurePosixPath(task['path']) / rel), tmdbid)
            stage(ctx, 'writeback', 'done', provider='OpenList', **receipt)
        state['metadata_complete'] = True
        runtime.services().store.save_task(ctx)
        library = settings.get('emby_library_id') or settings.get('emby_libraries', {}).get(next(iter(works)).split('/')[0])
        if settings.get('emby_config_id') and library:
            stage(ctx, 'emby', 'running', provider='LitePan', library_id=library)
            await lp.call('/api/admin/emby/refresh', {'config_id': settings['emby_config_id'], 'mode': 'library', 'library_id': library})
            stage(ctx, 'emby', 'done', provider='LitePan', library_id=library, accepted=True)
        else:
            raise ValueError('Select an existing LitePan Emby config and library for targeted refresh')
        state['complete'] = True
        ctx.log('LitePan STRM + single-work scrape, metadata writeback and LitePan Emby refresh complete')
        return True

    async def writeback(self, local, cloud, tmdbid):
        files = metadata_files(local)
        if not files or not any(f.suffix == '.nfo' for f in files):
            raise ValueError('Scraped NFO missing from mounted STRM directory')
        # Series-level identity must match before any upload; episodes use their own TMDB IDs.
        main = local / 'tvshow.nfo'
        if not main.exists():
            main = local / 'movie.nfo'
        if main.exists():
            tree = ET.fromstring(main.read_bytes())
            ids = [e.text for e in tree.findall(".//uniqueid[@type='tmdb']")] + [e.text for e in tree.findall('.//tmdbid')]
            if str(tmdbid) not in ids:
                raise ValueError('Scraped NFO TMDB identity mismatch')
        else:
            raise ValueError('Work-level identity NFO missing')
        token = os.getenv('OPENLIST_TOKEN', '')
        base = os.getenv('OPENLIST_URL', 'http://127.0.0.1:5244').rstrip('/')
        if not token:
            raise ValueError('OpenList metadata upload credential not configured')
        for file in files:
            relative = file.relative_to(local).as_posix()
            target = os.getenv('OPENLIST_MOUNT', '/115网盘').rstrip('/') + '/' + cloud.strip('/') + '/' + relative
            body = file.read_bytes()
            headers = {'Authorization': token}
            # Read-back comparison also reconciles an upload whose previous response was lost.
            async def remote_hash(file=file, target=target, headers=headers):
                listing = await api(base + '/api/fs/list', {'path': str(PurePosixPath(target).parent), 'page': 1, 'per_page': 1000, 'refresh': True}, headers=headers)
                if listing.get('code') != 200:
                    raise IntegrationError('Metadata cloud directory listing failed')
                entry = next((i for i in (listing.get('data') or {}).get('content', []) or [] if i.get('name') == file.name), None)
                if not entry:
                    return None
                info = await api(base + '/api/fs/get', {'path': target}, headers=headers)
                data = info.get('data') or {}
                url = data.get('raw_url', '')
                if info.get('code') != 200 or not url.startswith(('https://', 'http://')) or int(data.get('size', 0)) > 10 * 1024 * 1024:
                    raise IntegrationError('Metadata readback unavailable')
                def download_metadata():
                    try:
                        with urllib.request.urlopen(url, timeout=30) as response:
                            content = response.read(10 * 1024 * 1024 + 1)
                        if len(content) > 10 * 1024 * 1024:
                            raise IntegrationError('Metadata readback exceeds budget')
                        return hashlib.sha256(content).hexdigest()
                    except Exception:
                        raise IntegrationError('Metadata readback failed') from None
                return await asyncio.to_thread(download_metadata)
            wanted = hashlib.sha256(body).hexdigest()
            if await remote_hash() == wanted:
                continue
            def upload(body=body, target=target):
                req = urllib.request.Request(base + '/api/fs/put', data=body, method='PUT',
                      headers={'Authorization': token, 'File-Path': urllib.parse.quote(target, safe=''),
                               'Content-Type': 'application/octet-stream', 'As-Task': 'false'})
                try:
                    with urllib.request.urlopen(req, timeout=60) as response:
                        result = json.loads(response.read(65536))
                except Exception:
                    raise IntegrationError('Metadata upload result uncertain; inspect before retry') from None
                if result.get('code') != 200:
                    raise IntegrationError('OpenList metadata upload failed')
            await asyncio.to_thread(upload)
            if await remote_hash() != wanted:
                raise IntegrationError('Metadata readback digest mismatch')
        return {'files': len(files), 'bytes': sum(f.stat().st_size for f in files), 'sha256_verified': True}
