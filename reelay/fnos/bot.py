"""Telegram Bot API transport, restricted to a numeric owner in private chats."""
import asyncio
import os

from ..core import secret_store
from ..core.registry import register
from .http import api
from .intake import Intake
from .catalog import Catalog, infer
from .timeline import summary


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
            await call('sendMessage', {'chat_id': self.owner, 'text': text[:4000]})

    async def handle(self, update):
        callback = update.get('callback_query')
        message = update.get('message') or (callback or {}).get('message') or {}
        identity = {**message, 'from': callback.get('from')} if callback else message
        if not is_owner(identity, self.owner):
            return
        if callback:
            parts = str(callback.get('data', '')).split(':')
            if len(parts) == 4 and parts[0] == 's':
                result = await self.intake.confirm(parts[1], 'tg:' + self.owner, int(parts[2]), int(parts[3]))
                await self.send('订阅已确认；等待符合要求的资源。\n' + str(result['preferences']))
            await call('answerCallbackQuery', {'callback_query_id': callback['id']})
            return
        text = message.get('text', '').strip()
        if not text:
            return
        catalog = Catalog(self.service.store)
        if text == '/results':
            from .discovery import results, describe
            items = results(self.service.store)
            await self.send('\n\n'.join(describe(item) for item in items[:6]) or '暂无近期核验通过的其他网盘来源；/search 重新搜索。')
            return
        if text == '/integrations':
            from .integrations import check
            result = await check(self.service)
            await self.send('现有服务只读连通检查（不是任务执行回执）：\n' + '\n'.join(
                f"{name}：{'可用' if info.get('ready', info.get('configured')) else '未通过'}" for name, info in result['services'].items()) + '\n/job 任务ID 可查看每一步的执行结果。')
            return
        if text == '/sources':
            items = catalog.public()
            await self.send('\n'.join(f"{s['id']} {s['kind']} {s['url']}：{s['health']['state']}" for s in items) or '尚无来源。用 /source add 频道链接或文档链接 添加。')
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
            catalog.wake()
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
            await self.send('发送剧名和年份，确认作品与季数后自动追更。默认1080p、中文字幕、115云端转存、不重复。\n/status 真实进度\n/sources 来源\n/source add 链接 添加频道/文档\n/source add pansou http://地址:端口\n/search 立即搜索\n/audit 排除原因\n/job 任务ID 查看联动阶段\n/watch 720p any 剧名 调整偏好。')
            return
        if text in ('/status', '/subscriptions'):
            subs = self.service.store.subscriptions()
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
            await self.send(text)
            return
        prefs = None
        if text.startswith('/watch '):
            fields = text.split(maxsplit=3)
            if len(fields) != 4:
                raise ValueError('用法 /watch 1080p zh 剧名')
            _, resolution, subtitles, text = fields
            prefs = {'min_resolution': resolution, 'subtitles': subtitles}
        draft = await self.intake.preview(text, 'tg:' + self.owner, prefs)
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
                    except ValueError:
                        identity = update.get('message') or (update.get('callback_query') or {}).get('message') or {}
                        if is_owner(identity, self.owner):
                            await self.send('请求无效、已过期或缺少配置，请重新发送片名。')
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
                                     **({'parse_mode': 'HTML'} if html else {})})

    async def notify(self, ctx, event):
        owner = os.getenv('FNOS_TELEGRAM_OWNER_ID', '')
        if not owner.isdigit() or not secret_store.get('control_bot_token'):
            return
        if event in ('saved', 'pending_approval', 'failed', 'transfer_uncertain', 'organize_failed'):
            title = ctx.parsed.title if ctx.parsed else '资源'
            labels = {'saved': '云端转存完成', 'pending_approval': '需要人工核对，请打开管理台收件箱',
                      'failed': '处理失败，请打开管理台查看', 'transfer_uncertain': '转存结果待核验，未盲目重试', 'organize_failed': '整理需要人工核对'}
            await call('sendMessage', {'chat_id': owner, 'text': f"{title}：{labels[event]}；任务 {ctx.task_id}"})
