"""Cross-cloud discovery is separate from115 transfer. Public listings only."""
import hashlib
import re
import time
from urllib.parse import urlencode, urlsplit

from ..core.media import episode_of, is_extra, is_video
from .http import api


def share_identity(cloud, url):
    u = urlsplit(url)
    hosts = {'aliyun': ('www.alipan.com', 'alipan.com', 'www.aliyundrive.com', 'aliyundrive.com'),
             'quark': ('pan.quark.cn',)}
    if u.scheme != 'https' or u.netloc.lower() not in hosts.get(cloud, ()) or u.username or u.password:
        raise ValueError('Unsupported public share URL')
    match = re.fullmatch(r'/s/([A-Za-z0-9]+)', u.path.rstrip('/'))
    if not match:
        raise ValueError('Invalid public share path')
    return match[1]


def episode_facts(files):
    videos = []
    for file in files:
        name = file['name']
        if not is_video(name) or is_extra(name):
            continue
        season, episode = episode_of(name)
        if episode is None:
            match = re.match(r'^(\d{1,3})[_ .-]+(?:4K|HDR|1080|2160|H26[45])(?=[_ .-]|$)', name, re.I)
            episode = int(match[1]) if match else None
        if episode is not None:
            videos.append({'name': name[:240], 'size': file.get('size'), 'season': season, 'episode': episode})
    return videos


async def public_listing(cloud, url, password=''):
    identity = share_identity(cloud, url)
    files, queue, calls = [], ['root' if cloud == 'aliyun' else '0'], 0
    if cloud == 'aliyun':
        base = 'https://api.alipan.com'
        headers = {'Referer': 'https://www.alipan.com/', 'Origin': 'https://www.alipan.com'}
        result = await api(base + '/v2/share_link/get_share_token', {'share_id': identity, 'share_pwd': password or ''}, headers=headers)
        token = result.get('share_token')
        if not token:
            raise ValueError('Share cannot be read')
        headers['x-share-token'] = token
        while queue and calls < 12:
            parent = queue.pop(0)
            marker = ''
            while calls < 12:
                result = await api(base + '/adrive/v2/file/list_by_share', {'share_id': identity,
                    'parent_file_id': parent, 'limit': 100, 'marker': marker, 'order_by': 'name', 'order_direction': 'ASC'}, headers=headers)
                calls += 1
                if result.get('code'):
                    raise ValueError('Share listing rejected')
                for item in result.get('items', []):
                    if item.get('type') == 'folder':
                        queue.append(item['file_id'])
                    else:
                        files.append({'name': item.get('name', ''), 'size': item.get('size')})
                marker = result.get('next_marker')
                if not marker:
                    break
    else:
        base = 'https://drive-pc.quark.cn/1/clouddrive'
        headers = {'Referer': 'https://pan.quark.cn/', 'Origin': 'https://pan.quark.cn', 'User-Agent': 'Mozilla/5.0'}
        result = await api(base + '/share/sharepage/token?pr=ucpro&fr=pc', {'pwd_id': identity, 'passcode': password or ''}, headers=headers)
        token = (result.get('data') or {}).get('stoken')
        if result.get('code') != 0 or not token:
            raise ValueError('Share cannot be read')
        while queue and calls < 12:
            parent = queue.pop(0)
            for page in range(1, 5):
                if calls >= 12:
                    break
                params = {'pr':'ucpro', 'fr':'pc', 'pwd_id':identity, 'stoken':token, 'pdir_fid':parent,
                          'force':0, '_page':page, '_size':100, '_sort':'file_type:asc,file_name:asc'}
                result = await api(base + '/share/sharepage/detail?' + urlencode(params), headers=headers)
                calls += 1
                if result.get('code') != 0:
                    raise ValueError('Share listing rejected')
                items = (result.get('data') or {}).get('list', [])
                for item in items:
                    if item.get('dir'):
                        queue.append(item['fid'])
                    else:
                        files.append({'name':item.get('file_name', ''), 'size':item.get('size')})
                if len(items) < 100:
                    break
    return episode_facts(files)


async def discover(store, candidate, target, read=public_listing):
    share = candidate['share']
    cloud, url = share['cloud'], share['url']
    key = hashlib.sha256((cloud + url + str(target['tmdbid'])).encode()).hexdigest()[:24]
    previous = store.state_get('fnos_discoveries', key, {})
    if previous and time.time() - previous.get('checked_at', 0) < 600:
        return previous
    try:
        videos = await read(cloud, url, share.get('password', ''))
        episodes = sorted({v['episode'] for v in videos if v.get('season') in (None, target['season'])})
        if not episodes:
            raise ValueError('No verified regular episodes')
        result = {'id':key, 'title':target['title'], 'tmdbid':target['tmdbid'], 'season':target['season'],
                  'cloud':cloud, 'url':url, 'password':share.get('password', ''), 'episodes':episodes,
                  'files':videos[:100], 'state':'verified_files', 'checked_at':time.time(),
                  'transfer_state':'external_cloud_not_configured', 'video_downloaded':False}
        store.state_set('fnos_discoveries', key, result)
        return result
    except Exception as exc:
        result = {'id':key, 'state':'unverified', 'checked_at':time.time(), 'error':type(exc).__name__}
        store.state_set('fnos_discoveries', key, result)
        return result


def results(store):
    return [v for v in store.state_items('fnos_discoveries').values()
            if v.get('state') == 'verified_files' and time.time() - v['checked_at'] < 1800]


def describe(item):
    cloud = {'aliyun': '阿里云盘', 'quark': '夸克'}.get(item['cloud'], item['cloud'])
    return (f"{item['title']}：{cloud}实际文件最高第{max(item['episodes'])}集\n"
            + item['url'] + (f" 提取码：{item['password']}" if item.get('password') else '')
            + '\n仅核验文件；作品、画质和字幕待进一步确认。尚未配置此网盘转存，未下载视频中转。')
