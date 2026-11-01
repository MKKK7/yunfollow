"""Read-only Bot conversations grounded in task receipts and Emby inventory."""
import json
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from ..core import ai
from .http import api
from .timeline import summary

PAGE_SIZE = 8
LABELS = {'received': '等待识别', 'parsed': '已识别', 'pending_approval': '等待你确认资源',
          'queued': '等待处理', 'saving': '115云端转存中', 'stored': '已转存，等待整理',
          'organizing': '整理和联动处理中', 'need_manual': '需要人工处理', 'failed': '处理失败',
          'done': '本次流程完成', 'running': '正在执行', 'pending': '尚未开始',
          'waiting_confirmation': '等待确认', 'waiting_retry': '等待重试', 'skipped': '已跳过'}


def ranges(values):
    values = sorted(set(values))
    out = []
    for value in values:
        if out and value == out[-1][1] + 1:
            out[-1][1] = value
        else:
            out.append([value, value])
    return '、'.join(str(a) if a == b else f'{a}–{b}' for a, b in out) or '无'


def is_question(text, context=None):
    if text.startswith('/'):
        return text.split()[0] in ('/chat', '/recent', '/library')
    if re.match(r'^(?:帮我追|帮我找|给我找|我要看|我想看|订阅|追更|搜索)', text):
        return False
    if text.rstrip().endswith(('？', '?')) or re.search('是什么意思|为什么|你能做什么|你能干什么', text):
        return True
    if re.search(r'任务.*(?:进度|状态|如何|怎么样)|进度|最近.*(?:存|收|入库|添加)|库里|媒体库|片库|我.*(?:存了|收了|有什么)|你好|你是谁|你能做什么|你能干什么', text):
        return True
    if context and re.search(r'^(?:还有呢|还有什么|继续|下一页|上一页|更多|再看看|那.+呢|查一下.+|有没有.+)[？?。!！]*$', text):
        return True
    return False


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'query', 'days', 'page', 'media_type'}:
        raise ValueError('Invalid conversation plan')
    if value['kind'] not in ('progress', 'recent', 'library', 'help'):
        raise ValueError('Only read-only queries are allowed')
    if not isinstance(value['query'], str) or len(value['query']) > 200:
        raise ValueError('Invalid title filter')
    if type(value['days']) is not int or not 1 <= value['days'] <= 365 or type(value['page']) is not int or not 0 <= value['page'] <= 1000:
        raise ValueError('Invalid query window')
    if value['media_type'] not in ('', 'Movie', 'Series'):
        raise ValueError('Unsupported inventory type')
    return value


def context(store):
    value = store.state_get('yun_chat', 'context', {})
    return value.get('plan') if time.time() - value.get('updated_at', 0) < 1800 else None


async def plan(service, text):
    prior = context(service.store)
    clean = text.strip().rstrip('？?。!！')
    base = {'kind': 'library', 'query': '', 'days': 7, 'page': 0, 'media_type': ''}
    if prior and clean in ('还有呢', '继续', '下一页', '更多', '再看看'):
        return validate_plan({**prior, 'page': min(1000, prior['page'] + 1)})
    if prior and clean == '上一页':
        return validate_plan({**prior, 'page': max(0, prior['page'] - 1)})
    if clean in ('你好', '您好', '你是谁', '你能做什么', '你能干什么', '帮助'):
        return {**base, 'kind': 'help'}
    if clean.startswith('/recent'):
        return {**base, 'kind': 'recent', 'query': clean.removeprefix('/recent').strip()[:200]}
    if clean.startswith('/library'):
        return {**base, 'query': clean.removeprefix('/library').strip()[:200]}
    kind = None
    if re.search('进度|状态|到哪|完成了吗|卡在', clean):
        kind = 'progress'
    elif re.search('最近|近期|刚才|刚刚|今天|昨天|这周|本周|近.{0,3}天', clean) and re.search('存|收|入库|添加|下载|转存|更新', clean):
        kind = 'recent'
    elif re.search('库里|媒体库|片库|收藏|我有什么|我有哪些', clean):
        kind = 'library'
    value = {**base, 'kind': kind or (prior or base)['kind']}
    if re.search('今天|昨天', clean):
        value['days'] = 1 if '今天' in clean else 2
    match = re.search(r'(?:最近|近)(\d{1,3})天', clean)
    if match:
        value['days'] = min(365, max(1, int(match[1])))
    title = re.search(r'《([^》]{1,200})》|[“"]([^”"]{1,200})[”"]', clean)
    if title:
        value['query'] = next(v for v in title.groups() if v)
        if prior and not kind:
            return validate_plan({**prior, 'query': value['query'], 'page': 0})
    elif prior:
        follow = re.fullmatch(r'(?:那|查一下|查查|有没有)(.{1,100}?)(?:呢|吗)?', clean)
        if follow:
            return validate_plan({**prior, 'query': follow[1], 'page': 0})
    if kind and (title or re.fullmatch(r'(?:我|我的|现在|目前|最近|近期|任务|所有|的|了|都|这|些|存|保存|转存|下载|入库|收|内容|进度|状态|如何|怎么样|什么|哪些|有|里|库|媒体库|片库|情况|看看|一下|完成|最近\d+天|\s)+', clean)):
        if '电影' in clean:
            value['media_type'] = 'Movie'
        elif '剧集' in clean or '电视剧' in clean:
            value['media_type'] = 'Series'
        return validate_plan(value)
    # Complex questions are translated into filters; the model never writes the answer or calls actions.
    schema = {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['progress', 'recent', 'library', 'help']},
        'query': {'type': 'string'}, 'days': {'type': 'integer'}, 'page': {'type': 'integer'},
        'media_type': {'type': 'string', 'enum': ['', 'Movie', 'Series']}},
        'required': ['kind', 'query', 'days', 'page', 'media_type'], 'additionalProperties': False}
    try:
        client, model = service.ai_client()
        with service.pipeline.scope():
            result, _ = await ai.complete_json(client, model=model, purpose='yunfollow_readonly_chat',
                messages=[{'role': 'system', 'content': '只把用户问句转换为只读查询。progress查任务；recent查最近已保存或入库；library查Emby全库；help解释能力。query仅填作品名，不能填整句。默认7天、page=0、media_type空。接着问时可以沿用上次过滤条件。禁止新增订阅、搜索网盘、删除、移动或转存。上次条件：' + json.dumps(prior, ensure_ascii=False)},
                          {'role': 'user', 'content': text[:1000]}],
                schema=schema, name='readonly_query', validate=validate_plan, rule=service.pipeline.cfg.get('llm', {}), timeout=30)
        return result
    except (ai.AIError, ValueError, TypeError, KeyError, AttributeError):
        return validate_plan(value if kind else {**base, 'kind': 'help'})


async def emby_connection(service):
    from .media import LitePan
    from .verification import existing_connection
    lp = LitePan()
    await lp.login()
    settings = service.pipeline.cfg.get('plugins', {}).get('fnos_media', {})
    configs = await lp.call('/api/admin/emby/configs')
    snapshot = next((x for x in configs.get('items', []) if x['id'] == settings.get('emby_config_id')), None)
    connection = existing_connection(snapshot) if snapshot else None
    if not connection:
        raise ValueError('Emby query credential unavailable')
    return connection['emby_url'].rstrip('/'), {'X-Emby-Token': connection['api_key']}


async def emby_items(service, value, recent=False):
    base, headers = await emby_connection(service)
    params = {'Recursive': 'true', 'IncludeItemTypes': value['media_type'] or 'Movie,Series',
              'Fields': 'DateCreated,ProviderIds', 'SortBy': 'DateCreated' if recent else 'SortName',
              'SortOrder': 'Descending' if recent else 'Ascending', 'StartIndex': value['page'] * PAGE_SIZE,
              'Limit': PAGE_SIZE, 'EnableTotalRecordCount': 'true'}
    if value['query']:
        params['SearchTerm'] = value['query']
    if recent:
        # Filter actual returned timestamps; not all Emby versions support MinDateCreated.
        params.update(StartIndex=0, Limit=200)
    result = await api(base + '/Items?' + urlencode(params), headers=headers, timeout=20, limit=2 * 1024 * 1024)
    if not isinstance(result.get('Items'), list) or 'TotalRecordCount' not in result:
        raise ValueError('Emby inventory response invalid')
    items = result['Items']
    total = int(result['TotalRecordCount'])
    bounded = False
    unknown_dates = 0
    if recent:
        cutoff = datetime.now(timezone.utc) - timedelta(days=value['days'])
        selected = []
        for item in items:
            try:
                stamp = datetime.fromisoformat(str(item.get('DateCreated') or '').replace('Z', '+00:00'))
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                if stamp >= cutoff:
                    selected.append(item)
            except ValueError:
                unknown_dates += 1
                continue
        bounded = total > len(items)
        total = len(selected)
        items = selected[value['page'] * PAGE_SIZE:(value['page'] + 1) * PAGE_SIZE]
    return {'items': [{'name': str(x.get('Name') or '未命名')[:200], 'year': x.get('ProductionYear'),
                       'type': x.get('Type'), 'created': str(x.get('DateCreated') or '')[:40], 'id': str(x['Id'])}
                      for x in items], 'total': total, 'bounded': bounded, 'unknown_dates': unknown_dates}


def records(service, query=''):
    values = []
    for row in service.list_tasks(limit=200):
        ctx = service.store.get_task(row['task_id'])
        if not ctx or not ctx.raw.extra.get('fnos_subscription') or ctx.state.value in ('skipped', 'rejected'):
            continue
        if query and query.casefold() not in (ctx.parsed.title if ctx.parsed else ctx.raw.text).casefold():
            continue
        values.append((row, ctx))
    return values


def progress(service, value):
    values = records(service, value['query'])
    counts = Counter(ctx.state.value for _, ctx in values)
    active = sum(number for state, number in counts.items() if state not in ('done', 'pending_approval', 'need_manual', 'failed'))
    lines = [f"最近200条记录内：执行或排队 {active}，待确认 {counts['pending_approval']}，待处理/失败 {counts['need_manual']+counts['failed']}，完成 {counts['done']}。"]
    values.sort(key=lambda pair: (pair[1].state.value == 'done', -pair[0]['updated_at']))
    page = values[value['page'] * PAGE_SIZE:(value['page'] + 1) * PAGE_SIZE]
    for _, ctx in page:
        view = summary(ctx)
        lines.append(f"\n{view['title']}：{LABELS.get(view['state'], view['state'])}（{ctx.task_id}）")
        running = [v for v in view['stages'] if v['state'] in ('running', 'waiting_confirmation', 'waiting_retry', 'failed')]
        if running:
            lines.extend(f"  {v['label']}：{LABELS.get(v['state'], v['state'])}" for v in running)
            for item in running:
                receipt = item.get('receipt') or {}
                if item['id'] == 'verify' and receipt.get('emby_indexed') is False:
                    lines.append('  Emby尚未核验入库的集数：' + ranges(receipt.get('emby_pending_episodes', [])))
        elif view['state'] == 'done':
            lines.append('  本次处理完成；不代表作品已收齐。')
        if view['blocker'] and view['state'] != 'done':
            lines.append('  ' + view['blocker'][-160:])
    if not page:
        lines.append('没有匹配的任务记录。' if value['query'] else '目前没有需要展示的任务。')
    search = service.store.state_get('fnos_sources', 'progress', {})
    if search.get('message'):
        lines.append('\n最近搜索：' + str(search['message'])[:250])
    return '\n'.join(lines), len(values)


def recently_saved(service, value):
    result = []
    for row, ctx in records(service, value['query']):
        if ctx.meta.get('storage', {}).get('phase') != 'stored' or not ctx.meta.get('storage', {}).get('landed'):
            continue
        stamp = service.store._conn.execute("SELECT MIN(created_at) FROM dedup WHERE kind='share' AND task_id=?", (ctx.task_id,)).fetchone()[0]
        if stamp is None:
            # Creation time is labelled explicitly instead of claiming an inferred save time.
            stamp, label = row['created_at'], '任务创建'
        else:
            label = '115转存'
        if stamp >= time.time() - value['days'] * 86400:
            result.append((stamp, label, ctx))
    result.sort(key=lambda item: item[0], reverse=True)
    lines = [f"最近{value['days']}天，通过云追保存到115的内容："]
    for stamp, label, ctx in result[value['page'] * PAGE_SIZE:(value['page'] + 1) * PAGE_SIZE]:
        p = ctx.parsed
        lines.append(f"• {p.title} · 第{p.season}季 {ranges(p.episodes)}集\n  {label}：{time.strftime('%m-%d %H:%M', time.localtime(stamp))}；{LABELS.get(ctx.state.value, ctx.state.value)}")
    if len(lines) == 1:
        lines.append('没有符合时间范围的云追保存记录。这不代表你的网盘没有其他内容。')
    return '\n'.join(lines), len(result)


async def library(service, value, recent=False):
    try:
        found = await emby_items(service, value, recent)
        label = f"最近{value['days']}天 Emby新入库" if recent else 'Emby媒体库'
        lines = [f"{label}：{found['total']}部作品" + (f"（筛选：{value['query']}）" if value['query'] else '')]
        if found.get('bounded'):
            lines.append('时间筛选仅检查最新200条入库记录，以上为本次检索范围内数量。')
        for item in found['items']:
            kind = {'Movie': '电影', 'Series': '剧集'}.get(item['type'], item['type'] or '未知类型')
            lines.append(f"• {item['name']}" + (f" ({item['year']})" if item['year'] else '') + f" · {kind}" +
                         (f" · 入库{item.get('created', '')[:10]}" if recent else ''))
        if not found['items']:
            if recent and found.get('unknown_dates'):
                lines.append('返回条目缺少入库时间，无法确认时间范围内的结果；不等于最近没有入库。')
            else:
                lines.append('当前筛选下没有已收录作品。' if not value['page'] else '已经没有更多结果了。')
            if not recent and value['query']:
                managed = service.library_query(query=value['query'], limit=3)
                if managed.get('items'):
                    lines.append('但云追已有115转存记录：' + '、'.join(str(x.get('title') or '') for x in managed['items']) + '。尚未证明这些内容已被Emby收录。')
        if recent:
            lines.append('以上是Emby入库时间，不等同于115转存时间。')
        else:
            lines.append('这里查询的是Emby已收录内容，不代表115中尚未入库的全部文件。')
        return '\n'.join(lines), found['total'], 'emby'
    except Exception:
        # Network/authentication errors must never become an empty full-library result.
        found = service.library_query(query=value['query'] or None, limit=100)
        values = found.get('items', [])
        lines = ['暂时无法查询Emby，不能确认全库。以下仅为云追已处理的作品：']
        for item in values[value['page'] * PAGE_SIZE:(value['page'] + 1) * PAGE_SIZE]:
            lines.append('• ' + str(item.get('title') or '未知作品'))
        if not values:
            lines.append('暂无可用记录；没有把接口异常当成“库里没有”。')
        return '\n'.join(lines), len(values), 'yunfollow_only'


async def answer(service, text=None, value=None):
    value = validate_plan(value) if value else await plan(service, text or '')
    kind = value['kind']
    scope = 'yunfollow'
    if kind == 'progress':
        reply, total = progress(service, value)
    elif kind == 'recent':
        local, own_total = recently_saved(service, value)
        remote, remote_total, scope = await library(service, value, recent=True)
        reply, total = local + '\n\n' + remote, max(own_total, remote_total)
    elif kind == 'library':
        reply, total, scope = await library(service, value)
    else:
        reply, total = ('我是云追，可以直接问我“任务进度如何”“最近存了什么”“库里有什么”，也可以接着问“还有呢”“查一下《胆大党》”。\n'
                        '查询只读取任务记录和Emby，不会新增订阅、删除或转存。想追更时说“帮我追作品名”，再用卡片确认。'), 0
    service.store.state_set('yun_chat', 'context', {'plan': value, 'updated_at': time.time()})
    token = secrets.token_hex(8)
    service.store.state_set('yun_chat_pages', token, {'plan': value, 'updated_at': time.time()})
    buttons = []
    if value['page']:
        buttons.append({'text': '上一页', 'callback_data': 'chat:' + token + ':previous'})
    if (value['page'] + 1) * PAGE_SIZE < total:
        buttons.append({'text': '下一页', 'callback_data': 'chat:' + token + ':next'})
    buttons.append({'text': '刷新查询', 'callback_data': 'chat:' + token + ':refresh'})
    return {'text': reply[:3900], 'buttons': [buttons], 'kind': kind, 'scope': scope, 'total': total}


async def page(service, token, direction):
    value = service.store.state_get('yun_chat_pages', token)
    if not value or time.time() - value['updated_at'] > 1800:
        raise ValueError('查询卡片已过期，请重新提问')
    if direction not in ('next', 'previous', 'refresh'):
        raise ValueError('未知查询操作')
    query = value['plan']
    query['page'] = max(0, min(1000, query['page'] + {'next': 1, 'previous': -1, 'refresh': 0}[direction]))
    return await answer(service, value=query)
