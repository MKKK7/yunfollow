"""Telegram Bot API transport, restricted to a numeric owner in private chats."""
import asyncio
import os

from ..core import secret_store
from ..core.registry import register
from .http import api
from .intake import Intake
from .catalog import Catalog, infer
from .timeline import summary
from . import candidates, management


def is_owner(message, owner):
    return bool(str(owner).isdigit() and message.get('chat', {}).get('type') == 'private'
                and str(message.get('from', {}).get('id')) == str(owner)
                and str(message.get('chat', {}).get('id')) == str(owner))


async def call(method, payload):
    token = secret_store.get('control_bot_token')
    if not token:
        raise RuntimeError('Telegram Bot Token not configured')
    result = await api('https://api.telegram.org/bot' + token + '/' + method, payload, timeout=40)
    if not result.get('ok'):
        raise RuntimeError('Telegram API rejected request')
    return result.get('result')


class Bot:
    def __init__(self, service):
        self.service, self.intake = service, Intake(service)
        self.owner = os.getenv('FNOS_TELEGRAM_OWNER_ID', '')
        self.enabled = bool(self.owner.isdigit() and secret_store.get('control_bot_token'))
        self.closed = False

    async def start(self):
        pass

    def health(self):
        return {'status': 'connected' if self.enabled else 'disabled'}

    async def close(self):
        self.closed = True

    async def send(self, text, **kwargs):
        if self.enabled:
            return await call('sendMessage', {'chat_id': self.owner, 'text': text[:4000], **kwargs})

    async def show_batch(self, value=None):
        value = value or candidates.batch(self.service.store)
        self.service.store.state_set('yun_bot', 'batch', value)
        if not value['items']:
            await self.send('暂无核验通过的新115资源。会继续等待；可发送“重新搜索”。')
            return
        for index, item in enumerate(value['items'], 1):
            await self.send(f'候选 {index}/{len(value["items"])}\n' + candidates.card(item), reply_markup={'inline_keyboard': [
                [{'text': '选择此资源', 'callback_data': 'c:select:' + item['id']}, {'text': '忽略', 'callback_data': 'c:ignore:' + item['id']}],
                [{'text': '文件详情', 'callback_data': 'c:detail:' + item['id']}],
                [{'text': '下一批（忽略当前整批）', 'callback_data': 'b:next:' + value['id']}]]})

    async def handle(self, update):
        callback = update.get('callback_query')
        message = update.get('message') or (callback or {}).get('message') or {}
        identity = {**message, 'from': callback.get('from')} if callback else message
        if not is_owner(identity, self.owner):
            return
        if callback:
            await call('answerCallbackQuery', {'callback_query_id': callback['id']})
            parts = str(callback.get('data', '')).split(':')
            if len(parts) == 3 and parts[0] == 'chat':
                from .conversation import page
                result = await page(self.service, parts[1], parts[2])
                await self.send(result['text'], reply_markup={'inline_keyboard': result['buttons']})
            elif len(parts) == 2 and parts[0] == 'offsub':
                from .offline import create
                uri = self.service.store.state_get('yun_bot', 'offline_uri', '')
                value = create(self.service, uri, int(parts[1]))
                await self.send('仅提交给115云端离线，完成后核对实际文件。', reply_markup={'inline_keyboard': [[{'text': '确认提交115离线', 'callback_data': 'offline:confirm:' + value['id']}]]})
            elif len(parts) == 4 and parts[0] == 's':
                result = await self.intake.confirm(parts[1], 'tg:' + self.owner, int(parts[2]), int(parts[3]))
                await self.send('订阅已确认；等待符合要求的资源。\n' + str(result['preferences']))
            elif len(parts) == 3 and parts[0] == 'c':
                action, identity = parts[1:]
                if action == 'select':
                    result = await candidates.select(self.service, identity)
                    await self.send('资源已确认，开始115流程。任务 ' + result['task_id'], reply_markup={'inline_keyboard': [[{'text': '查看进度', 'callback_data': 'task:status:' + result['task_id']}]]})
                elif action == 'ignore':
                    candidates.ignore(self.service.store, identity)
                    await self.send('已忽略此资源版本；作品订阅保留。')
                elif action == 'detail':
                    item = self.service.store.state_get('yun_candidates', identity)
                    if not item:
                        raise ValueError('Candidate expired')
                    files = item.get('files', [])
                    for start in range(0, len(files), 15):
                        await self.send('\n'.join(f"{f['name']} · {int(f.get('size') or 0)/1024**2:.1f} MiB" for f in files[start:start+15]))
            elif len(parts) == 3 and parts[:2] == ['b', 'next']:
                await self.show_batch(candidates.next_batch(self.service.store, parts[2]))
            elif len(parts) == 3 and parts[0] == 'folder':
                from .classification import CATEGORIES, choose
                index = int(parts[2])
                if not 0 <= index < len(CATEGORIES):
                    raise ValueError('Invalid folder')
                await choose(self.service, parts[1], CATEGORIES[index])
                await self.send('目标已确认，将在115云端移动并继续后续处理。')
            elif len(parts) == 3 and parts[0] == 'task':
                if parts[1] == 'status':
                    ctx = self.service.store.get_task(parts[2])
                    if not ctx:
                        raise ValueError('Task not found')
                    await self.send('\n'.join(f"{v['label']}：{v['state']}" for v in summary(ctx)['stages']))
                else:
                    await management.task_action(self.service, parts[2], parts[1])
                    await self.send('操作已记录；不会重复转存已落地的视频。')
            elif len(parts) == 3 and parts[0] == 'offline':
                from .offline import confirm, ingest
                if parts[1] == 'confirm':
                    result = confirm(self.service, parts[2])
                    await self.send('已确认提交115云端离线，任务状态：' + result['state'])
                elif parts[1] == 'files':
                    value = self.service.store.state_get('yun_offline', parts[2])
                    if not value:
                        raise ValueError('Offline task not found')
                    await self.send('请确认这些文件确为订阅作品；无季集证据的文件不会整理。\n' + '\n'.join(f"{f['name']} · {f['size']/1024**2:.1f} MiB" for f in value.get('files', [])[:30]),
                        reply_markup={'inline_keyboard': [[{'text': '确认实际文件，继续整理', 'callback_data': 'offline:ingest:' + parts[2]}]]})
                elif parts[1] == 'ingest':
                    result = await ingest(self.service, parts[2])
                    await self.send('实际文件已确认，继续云端整理与刮削。任务 ' + result['task_id'])
            return
        text = message.get('text', '').strip()
        if not text:
            return
        from .conversation import is_question, context, answer
        if is_question(text, context(self.service.store)):
            result = await answer(self.service, text.removeprefix('/chat').strip())
            await self.send(result['text'], reply_markup={'inline_keyboard': result['buttons']})
            return
        if text.startswith(('magnet:?', 'ed2k://')):
            from .offline import create
            subs = self.service.store.subscriptions()
            if len(subs) != 1:
                self.service.store.state_set('yun_bot', 'offline_uri', text)
                await self.send('请选择此离线资源对应的订阅作品。', reply_markup={'inline_keyboard': [[{'text': s['title'] + ' 第' + str(s['season']) + '季', 'callback_data': 'offsub:' + str(s['id'])}] for s in subs[:15]]})
                return
            value = create(self.service, text, subs[0]['id'])
            await self.send('仅提交给115云端离线；文件完成后需要核对作品和季集。', reply_markup={'inline_keyboard': [[{'text': '确认提交115离线', 'callback_data': 'offline:confirm:' + value['id']}]]})
            return
        requested_text = text
        from .intent import interpret
        text = await interpret(self.service, text)
        if text.startswith('/chat '):
            result = await answer(self.service, text.removeprefix('/chat ').strip())
            await self.send(result['text'], reply_markup={'inline_keyboard': result['buttons']})
            return
        catalog = Catalog(self.service.store)
        if text == '/offline':
            from .offline import jobs
            values = jobs(self.service.store)
            if not values:
                await self.send('暂无离线草稿。可发送磁力或电驴链接，或等待现有来源发现。只提交给115云端。')
            for value in values[-3:]:
                buttons = []
                if value['state'] == 'waiting_confirmation':
                    buttons = [[{'text': '确认提交115云端离线', 'callback_data': 'offline:confirm:' + value['id']}]]
                elif value['state'] == 'waiting_file_confirmation':
                    buttons = [[{'text': '核对实际文件', 'callback_data': 'offline:files:' + value['id']}]]
                await self.send(f"{value['subscription']['title']} · 115离线\n状态：{value['state']}\n来源：{value.get('source_name') or '手动提供'}\n{value.get('evidence') or ''}\n磁力可用性需要115实际反馈，不等于已核验通过的分享。",
                                reply_markup={'inline_keyboard': buttons})
            return
        if text == '/results':
            await self.show_batch()
            return
        if text == '/next':
            value = self.service.store.state_get('yun_bot', 'batch', {})
            await self.show_batch(candidates.next_batch(self.service.store, value['id']) if value else None)
            return
        if text.startswith(('/choose ', '/ignore ')):
            value = self.service.store.state_get('yun_bot', 'batch', {})
            index = int(text.split()[1]) - 1
            if not 0 <= index < len(value.get('items', [])):
                raise ValueError('Please request a current batch')
            item = value['items'][index]
            if text.startswith('/choose'):
                await self.send('请确认选择此115资源。', reply_markup={'inline_keyboard': [[{'text': '确认选择', 'callback_data': 'c:select:' + item['id']}]]})
            else:
                candidates.ignore(self.service.store, item['id'])
                await self.send('已忽略此版本，订阅保留。')
            return
        if text.startswith('/retry'):
            fields = text.split()
            if len(fields) != 2:
                items = management.tasks(self.service, waiting=True)
                await self.send('选择需要处理的任务。', reply_markup={'inline_keyboard': [[{'text': item['title'] or item['task_id'], 'callback_data': 'task:retry:' + item['task_id']}] for item in items[:8]]})
            else:
                await self.send('确认只重试失败阶段？', reply_markup={'inline_keyboard': [[{'text': '确认重试', 'callback_data': 'task:retry:' + fields[1]}]]})
            return
        if text == '/confirm':
            draft = self.service.store.state_get('yun_bot', 'draft', {})
            buttons = [[{'text': f"确认 {v['title']} 第{s['season']}季", 'callback_data': f"s:{draft['id']}:{v['tmdbid']}:{s['season']}"}] for v in draft.get('candidates', [])[:5] for s in v.get('seasons', [])[:5] if s['season'] > 0]
            await self.send('请核对作品和季数。' if buttons else '暂无待确认作品，请先发剧名。', reply_markup={'inline_keyboard': buttons})
            return
        if text == '/integrations':
            from .integrations import check
            result = await check(self.service)
            await self.send('现有服务只读连通检查（不是任务执行回执）：\n' + '\n'.join(
                f"{name}：{'可用' if info.get('ready', info.get('configured')) else '未通过'}" for name, info in result['services'].items()) + '\n/job 任务ID 可查看每一步的执行结果。')
            return
        if text == '/sources':
            items = catalog.public()
            await self.send('\n'.join(f"{s.get('name') or s['kind']} {s['url']}：{s['health']['state']}\n{s.get('note') or ''}" for s in items) or '尚无来源。发送频道或文档链接即可添加。')
            return
        if text.startswith('/source '):
            fields = text.split()
            if len(fields) == 3 and fields[1] == 'remove':
                catalog.remove(fields[2])
                await self.send('来源已移除。')
                return
            if len(fields) in (3, 4) and fields[1] == 'add':
                url = fields[-1]
                item = catalog.add(fields[2] if len(fields) == 4 else infer(url), url)
                await self.send(f"已添加 {item['kind']} 来源，ID：{item['id']}。文档来源需要同步授权；/sources 查看状态。")
                return
            raise ValueError('Use /source add URL or /source add pansou http://host:port')
        if text.startswith(('https://t.me/', 'https://www.kdocs.cn/l/', 'https://kdocs.cn/l/')) and ' ' not in text:
            item = catalog.add(infer(text), text)
            await self.send(f"已添加搜索来源 {item['kind']}，ID：{item['id']}。/sources 查看同步状态。")
            return
        if text == '/search':
            management.wake(self.service)
            await self.send('已安排对当前已确认订阅执行自动搜索。/status 查看轮次和排除统计。')
            return
        if text == '/audit':
            values = list(self.service.store.state_get('fnos_preflight', 'audit', {}).values())[:12]
            await self.send('\n'.join(f"{v['title'][:50]}：{v['verdict']} / {v['reason']}" for v in values) or '尚无核验记录。失效链接不会进入任务列表。')
            return
        if text.startswith('/job '):
            task_id = text.split(maxsplit=1)[1]
            ctx = self.service.store.get_task(task_id)
            if not ctx or not ctx.raw.extra.get('fnos_preflight'):
                raise ValueError('Qualified job not found')
            view = summary(ctx)
            await self.send(f"{view['title']}：{view['state']}\n" + '\n'.join(f"{s['label']}：{s['state']}" for s in view['stages']) + '\n' + (view['blocker'] or ''))
            return
        if text in ('/start', '/help'):
            await self.send('可以直接和我对话：\n“任务进度如何？”\n“我最近存了什么？”\n“我库里有什么？”\n接着问“还有呢”“查一下《胆大党》”，也可以点按钮翻页。\n\n想追更时发送剧名和年份，确认作品与资源后处理。默认1080p、中文字幕、只走115、不重复。\n/status 进度\n/recent 最近保存\n/library 媒体库\n/sources 来源\n/search 搜索\n/results 候选\n/job 任务ID 联动阶段。')
            return
        if text in ('/status', '/subscriptions'):
            subs = management.subscriptions(self.service)
            progress = self.service.store.state_get('fnos_sources', 'progress', {}) or {}
            text = '\n'.join(f"{s['title']} 第{s['season']}季（订阅状态，非下载进度）" for s in subs) or '尚无订阅。'
            if progress:
                text += '\n自动搜索：' + str(progress.get('message', progress.get('state', '')))
                text += f"\n本轮匹配 {progress.get('matched', 0)}，提交核验 {progress.get('emitted', 0)}。"
                text += f"已检查 {progress.get('checked', 0)}，排除 {progress.get('excluded', 0)}，暂无法核验 {progress.get('deferred', 0)}。"
            counts = self.service.store.counts_by_state()
            text += f"\n任务汇总：待识别 {counts.get('received', 0)}，待确认 {counts.get('pending_approval', 0)}，等待转存 {counts.get('queued', 0)}，转存中 {counts.get('saving', 0)}，完成 {counts.get('done', 0)}。"
            rows = self.service.list_tasks(state='received,queued,saving,stored,organizing,pending_approval,need_manual,failed', limit=5)
            if rows:
                text += '\n最近任务：' + '；'.join(f"{r.get('title') or '候选'}：{r['state']}" for r in rows)
            for sub in subs:
                text += f"\n{sub['title']}：已收{sub['have']}；缺集{sub['missing'] if sub['airing'].get('state') == 'verified' else '播出信息待核验'}"
            await self.send(text)
            return
        from .intake import preferences_from_text
        prefs = preferences_from_text(requested_text) or None
        if text.startswith('/watch '):
            fields = text.split(maxsplit=3)
            if len(fields) != 4:
                raise ValueError('用法 /watch 1080p zh 剧名')
            _, resolution, subtitles, text = fields
            prefs = {'min_resolution': resolution, 'subtitles': subtitles}
        draft = await self.intake.preview(text, 'tg:' + self.owner, prefs)
        self.service.store.state_set('yun_bot', 'draft', draft)
        buttons = []
        for candidate in draft['candidates'][:5]:
            for season in candidate.get('seasons', [])[:10]:
                number = int(season['season'])
                if number > 0:
                    buttons.append([{'text': f"确认 {candidate['title']} ({candidate.get('year', '')}) 第{number}季",
                                     'callback_data': f"s:{draft['id']}:{candidate['tmdbid']}:{number}"}])
        await call('sendMessage', {'chat_id': self.owner,
                   'text': '请核对作品与季数；确认后自动追更。默认1080p、中文字幕、不覆盖已有集。' if buttons else '未找到可靠结果，请补充片名、年份。',
                   'reply_markup': {'inline_keyboard': buttons}})

    async def run_forever(self):
        offset = self.service.store.state_get('fnos_bot', 'offset', 0)
        while not self.closed:
            try:
                updates = await call('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})
                for update in updates:
                    try:
                        await self.handle(update)
                    except ValueError as exc:
                        identity = update.get('message') or (update.get('callback_query') or {}).get('message') or {}
                        if update.get('callback_query'):
                            identity = {**identity, 'from': update['callback_query'].get('from')}
                        if is_owner(identity, self.owner):
                            from ..core.ai import scrub
                            await self.send('操作未执行：' + scrub(str(exc))[:250] + '\n可以发送“状态”查看当前任务。')
                    # Unknown network outcomes are retried with a durable draft confirmation.
                    offset = update['update_id'] + 1
                    self.service.store.state_set('fnos_bot', 'offset', offset)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(10)


@register(kind='provider', ptype='notify')
class FnosNotify:
    name = 'fnos_bot'

    async def notice(self, text, html=False, buttons=None):
        owner = os.getenv('FNOS_TELEGRAM_OWNER_ID', '')
        if owner.isdigit() and secret_store.get('control_bot_token'):
            await call('sendMessage', {'chat_id': owner, 'text': text[:4000],
                                     **({'parse_mode': 'HTML'} if html else {}),
                                     **({'reply_markup': {'inline_keyboard': buttons}} if buttons else {})})

    async def notify(self, ctx, event):
        owner = os.getenv('FNOS_TELEGRAM_OWNER_ID', '')
        if not owner.isdigit() or not secret_store.get('control_bot_token'):
            return
        if event in ('saved', 'pending_approval', 'failed', 'transfer_uncertain', 'organize_failed'):
            title = ctx.parsed.title if ctx.parsed else '资源'
            labels = {'saved': '本次云端处理完成；是否收齐请查看订阅缺集', 'pending_approval': '需要确认资源版本',
                      'failed': '处理失败，可查看进度并重试失败阶段', 'transfer_uncertain': '转存结果待核验，未盲目重试', 'organize_failed': '分类或整理需要人工核对'}
            buttons = [[{'text': '查看各步进度', 'callback_data': 'task:status:' + ctx.task_id}]]
            if event == 'pending_approval':
                buttons.append([{'text': '确认此资源', 'callback_data': 'task:approve:' + ctx.task_id}, {'text': '忽略', 'callback_data': 'task:ignore:' + ctx.task_id}])
            if event in ('failed', 'organize_failed'):
                buttons.append([{'text': '重试失败阶段', 'callback_data': 'task:retry:' + ctx.task_id}])
            labels['pending_approval'] = '等待资源版本确认'
            await call('sendMessage', {'chat_id': owner, 'text': f"{title}：{labels[event]}；任务 {ctx.task_id}", 'reply_markup': {'inline_keyboard': buttons}})
