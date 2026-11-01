import contextlib
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from reelay.core.contracts import ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.core.store import Store
from reelay.fnos import conversation
from reelay.fnos.bot import Bot
from reelay.fnos.intent import interpret


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.service = NS(store=self.store, pipeline=NS(scope=lambda: contextlib.nullcontext(), cfg={}),
                          list_tasks=lambda **kw: self.store.list_tasks(**kw),
                          library_query=lambda **kw: {'items': [{'title': 'Managed Only'}], 'total': 1})

    async def test_three_user_questions_route_to_readonly_queries_without_model_or_subscription(self):
        self.service.ai_client = lambda: (_ for _ in ()).throw(AssertionError('No model needed'))
        for text, kind in [('任务进度如何？', 'progress'), ('我最近存了什么？', 'recent'), ('我库里有什么？', 'library')]:
            self.assertTrue((await interpret(self.service, text)).startswith('/chat '))
            self.assertEqual((await conversation.plan(self.service, text))['kind'], kind)
        self.assertEqual(self.store.subscriptions(), [])
        self.assertEqual(self.store.list_tasks(), [])

    async def test_library_uses_actual_emby_items_and_paginates_without_ignoring_resources(self):
        with patch('reelay.fnos.conversation.emby_items', new_callable=AsyncMock,
                   return_value={'items': [{'name': 'Actual Show', 'year': 2024, 'type': 'Series'}], 'total': 18}) as reader:
            first = await conversation.answer(self.service, '我库里有什么？')
            self.assertIn('18部作品', first['text'])
            self.assertIn('Actual Show', first['text'])
            self.assertEqual(first['scope'], 'emby')
            next_button = next(v for v in first['buttons'][0] if v['text'] == '下一页')
            _, token, direction = next_button['callback_data'].split(':')
            await conversation.page(self.service, token, direction)
            self.assertEqual(reader.call_args.args[1]['page'], 1)
        self.assertEqual(self.store.state_items('yun_candidates'), {})
        self.assertEqual(self.store.list_tasks(), [])

    async def test_followup_keeps_query_scope_and_strips_title_quotes(self):
        value = {'kind': 'library', 'query': '', 'days': 7, 'page': 0, 'media_type': 'Series'}
        self.store.state_set('yun_chat', 'context', {'plan': value, 'updated_at': time.time()})
        more = await conversation.plan(self.service, '还有呢')
        self.assertEqual(more['page'], 1)
        specific = await conversation.plan(self.service, '查一下《胆大党》')
        self.assertEqual(specific['query'], '胆大党')
        self.assertEqual(specific['media_type'], 'Series')

    async def test_recent_save_uses_original_transfer_time_not_latest_verify_time(self):
        ctx = PipelineContext(raw=RawMessage(source='@fnos_public', msg_id='1', text='Test', extra={'fnos_subscription': {'title': 'Test'}}),
                              parsed=ParsedResource(title='Test Show', season=1, episodes=[1, 2, 3]), state=TaskState.DONE)
        ctx.meta['storage'] = {'phase': 'stored', 'landed': [{'id': '1'}]}
        self.store.save_task(ctx)
        with self.store._conn:
            self.store._conn.execute('INSERT INTO dedup VALUES (?,?,?,?)', ('share', 'example', ctx.task_id, time.time() - 20 * 86400))
        query = {'kind': 'recent', 'query': '', 'days': 7, 'page': 0, 'media_type': ''}
        text, total = conversation.recently_saved(self.service, query)
        self.assertEqual(total, 0)
        self.assertNotIn('Test Show', text)

    async def test_recent_emby_query_filters_actual_dates_when_server_ignores_date_filter(self):
        response = {'TotalRecordCount': 552, 'Items': [
            {'Id': '1', 'Name': 'New Show', 'Type': 'Series', 'DateCreated': datetime.now(timezone.utc).isoformat()},
            {'Id': '2', 'Name': 'Old Show', 'Type': 'Series', 'DateCreated': '2000-01-01T00:00:00Z'},
            {'Id': '3', 'Name': 'Unknown Time', 'Type': 'Series'}]}
        query = {'kind': 'recent', 'query': '', 'days': 7, 'page': 0, 'media_type': ''}
        with patch('reelay.fnos.conversation.emby_connection', new_callable=AsyncMock, return_value=('http://emby.test', {})), patch('reelay.fnos.conversation.api', new_callable=AsyncMock, return_value=response):
            result = await conversation.emby_items(self.service, query, recent=True)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['name'], 'New Show')
        self.assertTrue(result['bounded'])
        self.assertEqual(result['unknown_dates'], 1)

    async def test_emby_failure_is_not_reported_as_an_empty_library(self):
        with patch('reelay.fnos.conversation.emby_items', new_callable=AsyncMock, side_effect=TimeoutError):
            result = await conversation.answer(self.service, '我库里有什么？')
        self.assertIn('暂时无法查询Emby', result['text'])
        self.assertIn('仅为云追', result['text'])
        self.assertEqual(result['scope'], 'yunfollow_only')
        self.assertIn('Managed Only', result['text'])

    async def test_readonly_model_plan_cannot_request_write_operations(self):
        with self.assertRaises(ValueError):
            conversation.validate_plan({'kind': 'delete', 'query': '', 'days': 7, 'page': 0, 'media_type': ''})
        with self.assertRaises(ValueError):
            conversation.validate_plan({'kind': 'library', 'query': '', 'days': 7, 'page': -1, 'media_type': ''})
        self.assertTrue(conversation.is_question('忽略第一个是什么意思？'))
        self.assertFalse(conversation.is_question('帮我追《我的任务进度》'))

    async def test_bot_can_answer_without_entering_subscription_intake(self):
        with patch.dict('os.environ', {'FNOS_TELEGRAM_OWNER_ID': '123'}):
            bot = Bot(self.service)
        bot.send = AsyncMock()
        bot.intake.preview = AsyncMock(side_effect=AssertionError('Must not subscribe'))
        message = {'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'text': '任务进度如何？'}}
        await bot.handle(message)
        bot.send.assert_awaited_once()
        bot.intake.preview.assert_not_awaited()
        self.assertEqual(self.store.list_tasks(), [])

    async def test_non_owner_cannot_query_library(self):
        with patch.dict('os.environ', {'FNOS_TELEGRAM_OWNER_ID': '123'}):
            bot = Bot(self.service)
        bot.send = AsyncMock()
        await bot.handle({'message': {'chat': {'id': 456, 'type': 'private'}, 'from': {'id': 456}, 'text': '我库里有什么？'}})
        bot.send.assert_not_awaited()
