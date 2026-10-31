import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reelay.core.contracts import ParsedResource, PipelineContext, RawMessage, Decision
from reelay.core.store import Store
from reelay.fnos.anthropic import translate
from reelay.fnos.api import router
from reelay.fnos.bot import Bot, is_owner
from reelay.fnos.intake import Intake, preferences
from reelay.fnos.media import metadata_files, safe_relative, FnosMedia
from reelay.fnos.policy import quality, FnosDecision
from reelay.fnos.sources import matching


class Service:
    def __init__(self, store):
        self.store = store
        self.pipeline = NS(reload_config=lambda: None)
        self.calls = 0

    async def resolve_subscription(self, text):
        return {'candidates': [{'tmdbid': 62126, 'title': 'Luke Cage', 'year': 2016, 'seasons': [{'season': 1}]}]}

    async def subscribe_show(self, tmdbid, season):
        self.calls += 1
        record = self.store.add_subscription(f'tmdb:{tmdbid}:S{season}', {'tmdbid': tmdbid, 'title': 'Luke Cage', 'season': season, 'auto': True})
        return {'subscription': record}

    def list_tasks(self, **kwargs):
        return []


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.db')
        self.store = Store(self.path)
        self.service = Service(self.store)
        self.intake = Intake(self.service)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_preview_never_subscribes(self):
        await self.intake.preview('Luke Cage', 'owner')
        self.assertEqual(self.store.subscriptions(), [])

    async def test_confirm_survives_restart_and_replay(self):
        draft = await self.intake.preview('Luke Cage', 'owner')
        result = await self.intake.confirm(draft['id'], 'owner', 62126, 1)
        self.store.close()
        self.store = Store(self.path)
        self.service = Service(self.store)
        intake = Intake(self.service)
        self.assertEqual(await intake.confirm(draft['id'], 'owner', 62126, 1), result)
        self.assertEqual(self.service.calls, 0)
        self.assertEqual(len(self.store.subscriptions()), 1)

    async def test_draft_owner_expiry_and_candidate_are_checked(self):
        draft = await self.intake.preview('Luke Cage', 'owner')
        for owner, tmdb, season in [('stranger', 62126, 1), ('owner', 1, 1), ('owner', 62126, 99)]:
            with self.assertRaises(ValueError):
                await self.intake.confirm(draft['id'], owner, tmdb, season)
        record = self.store.state_get('fnos_drafts', draft['id'])
        record['expires_at'] = time.time() - 1
        self.store.state_set('fnos_drafts', draft['id'], record)
        with self.assertRaises(ValueError):
            await self.intake.confirm(draft['id'], 'owner', 62126, 1)
        self.assertEqual(self.store.subscriptions(), [])

    async def test_concurrent_confirmation_creates_once(self):
        draft = await self.intake.preview('Luke Cage', 'owner')
        await asyncio.gather(*(self.intake.confirm(draft['id'], 'owner', 62126, 1) for _ in range(5)))
        self.assertEqual(self.service.calls, 1)

    async def test_interrupted_after_subscription_before_draft_commit(self):
        draft = await self.intake.preview('Luke Cage', 'owner')
        await self.service.subscribe_show(62126, 1)
        await self.intake.confirm(draft['id'], 'owner', 62126, 1)
        self.assertEqual(self.service.calls, 1)

    async def test_bot_ignores_strangers_without_even_replying(self):
        bot = Bot(self.service)
        bot.owner = '123'
        with patch('reelay.fnos.bot.call', new_callable=AsyncMock) as call:
            await bot.handle({'message': {'from': {'id': 456}, 'chat': {'type': 'private', 'id': 456}, 'text': 'Luke Cage'}})
            call.assert_not_awaited()
        self.assertEqual(self.store.subscriptions(), [])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.service = Service(self.store)
        self.app = FastAPI()
        self.app.include_router(router(self.service))
        self.client = TestClient(self.app)
        self.env = patch.dict(os.environ, {'FNOS_PROPOSAL_KEY': 'p' * 32, 'FNOS_OWNER_KEY': 'o' * 32})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.store.close()

    def test_api_requires_credentials_and_model_key_cannot_confirm(self):
        self.assertEqual(self.client.get('/fnos/v1/status').status_code, 401)
        headers = {'Authorization': 'Bearer ' + 'p' * 32}
        self.assertEqual(self.client.get('/fnos/v1/status', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/fnos/v1/drafts/x/confirm', headers=headers, json={'tmdbid': 62126, 'season': 1}).status_code, 401)

    def test_extra_fields_and_non_integer_ids_rejected(self):
        headers = {'Authorization': 'Bearer ' + 'o' * 32}
        self.assertEqual(self.client.post('/fnos/v1/drafts/x/confirm', headers=headers, json={'tmdbid': 1, 'season': 1, 'shell': 'rm'}).status_code, 422)
        self.assertEqual(self.client.post('/fnos/v1/drafts/x/confirm', headers=headers, json={'tmdbid': True, 'season': '1'}).status_code, 422)

    def test_same_owner_and_proposal_keys_fail_closed(self):
        with patch.dict(os.environ, {'FNOS_OWNER_KEY': 'p' * 32}):
            self.assertEqual(self.client.post('/fnos/v1/drafts/x/confirm', headers={'Authorization': 'Bearer ' + 'p' * 32}, json={'tmdbid': 1, 'season': 1}).status_code, 401)


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_share_never_enters_approval_or_transfer(self):
        ctx = PipelineContext(raw=RawMessage(source='@test', msg_id='expired', text='expired'), meta={'share': {'status': 'unavailable'}})
        self.assertEqual(await FnosDecision().decide(ctx), Decision.SKIP)

    async def test_movie_candidate_cannot_satisfy_confirmed_tv_subscription(self):
        ctx = PipelineContext(raw=RawMessage(source='@test', msg_id='movie', text='罗小黑战记 2019', extra={'fnos_subscription': {'media_type': 'tv', 'tmdbid': 88319}}), parsed=ParsedResource(media_type='movie', title='罗小黑战记'))
        self.assertEqual(await FnosDecision().decide(ctx), Decision.SKIP)

    async def test_tmdb_movie_cannot_be_relabelled_as_anime_to_match_subscription(self):
        ctx = PipelineContext(raw=RawMessage(source='@test', msg_id='animated-movie', text='罗小黑战记 2019', extra={'fnos_subscription': {'tmdbid': 88319}}), parsed=ParsedResource(media_type='anime', title='罗小黑战记'), meta={'tmdb': {'matched': True, 'kind': 'movie', 'tmdbid': 620249}})
        self.assertEqual(await FnosDecision().decide(ctx), Decision.SKIP)

    async def test_json_mode_uses_runtime_config_and_validates_reasoning_wrapped_reply(self):
        from reelay.core.ai import complete_json
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content='<think>analysis</think>\n{"title":"罗小黑战记","year":null}', refusal=None))], usage=None))
        client = NS(chat=NS(completions=NS(create=create)))
        def cfg(path, default=None):
            return {'llm_response_format': 'json_object'} if path == 'defaults' else default
        with patch('reelay.core.ai.runtime.get', side_effect=cfg):
            result, _ = await complete_json(client, model='MiniMax-M3', purpose='test', messages=[{'role':'user','content':'test'}], schema={'type':'object'}, name='test', validate=lambda d:d)
        self.assertEqual(result['title'], '罗小黑战记')
        self.assertEqual(create.call_args.kwargs['response_format'], {'type': 'json_object'})

    async def test_upstream_review_cannot_be_upgraded(self):
        parsed = ParsedResource(title='Luke Cage', season=1, media_type='tv', resolution='1080p', release={'subtitles': ['chs']})
        ctx = PipelineContext(raw=RawMessage(source='@test', msg_id='1', text='test'), parsed=parsed)
        services = NS(subscriptions=lambda: [{'title': 'Luke Cage', 'season': 1, 'tmdbid': 62126, 'auto': True}], store=NS(state_get=lambda *a: {}))
        ctx.meta['tmdb'] = {'matched': True, 'tmdbid': 62126}
        with patch('reelay.providers.decision.SubscriptionDecision.decide', new_callable=AsyncMock, return_value=Decision.ASK), patch('reelay.fnos.policy.runtime.services', return_value=services):
            self.assertEqual(await FnosDecision().decide(ctx), Decision.ASK)

    async def test_media_refuses_unverified_cloud_organize_without_network(self):
        ctx = PipelineContext(raw=RawMessage(source='@test', msg_id='1', text='test'))
        with self.assertRaises(ValueError), patch('reelay.fnos.media.LitePan.login', new_callable=AsyncMock) as login:
            await FnosMedia().run(ctx)
        login.assert_not_awaited()


class UtilityTests(unittest.TestCase):
    def test_structured_json_wrappers_do_not_allow_arbitrary_prose_or_trailing_instructions(self):
        from reelay.core.ai import structured_payload
        self.assertEqual(structured_payload('<think>analysis</think>\n```json\n{"a":1}\n```'), {'a':1})
        for invalid in ('text {"a":1}', '<think>unclosed', '{"a":1} trailing', '{}{}'):
            with self.assertRaises(ValueError):
                structured_payload(invalid)

    def test_automatic_search_includes_series_variants(self):
        from reelay.fnos.sources import search_queries
        self.assertIn('罗小黑战记 剧集', search_queries({'title':'罗小黑战记'}))

    def test_quality_unknown_and_missing_subtitles_need_review(self):
        self.assertEqual(quality(ParsedResource(resolution=None), {}), 'review')
        self.assertEqual(quality(ParsedResource(resolution='1080p'), {}), 'review')
        self.assertEqual(quality(ParsedResource(resolution='720p'), {}), 'reject')
        self.assertEqual(quality(ParsedResource(resolution='1080p', release={'subtitles': ['chs']}), {}), 'accept')

    def test_preferences_forbid_overwrite_and_unknown_fields(self):
        for value in ({'replace_existing': True}, {'execute': 'shell'}, {'min_resolution': '8k'}):
            with self.assertRaises(ValueError):
                preferences(value)

    def test_metadata_selection_excludes_video_and_limits_size(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'show.mkv').write_bytes(b'video')
            (root / 'tvshow.nfo').write_text('<tvshow/>')
            self.assertEqual([p.name for p in metadata_files(root)], ['tvshow.nfo'])
            with (root / 'poster.jpg').open('wb') as output:
                output.truncate(11 * 1024 * 1024)
            with self.assertRaises(ValueError):
                metadata_files(root)

    def test_paths_cannot_escape(self):
        for value in ('../etc', '/etc', 'foo\\bar'):
            with self.assertRaises(ValueError):
                safe_relative(value)

    def test_private_owner_only(self):
        msg = {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        self.assertTrue(is_owner(msg, '123'))
        msg['chat']['type'] = 'group'
        self.assertFalse(is_owner(msg, '123'))

    def test_anthropic_translation_preserves_tool_results_and_schema(self):
        translated = translate({'model': 'test', 'messages': [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'request'}, {'role': 'assistant', 'tool_calls': [{'id': '1', 'function': {'name': 'query', 'arguments': '{"q":"show"}'}}]}, {'role': 'tool', 'tool_call_id': '1', 'content': '{}'}], 'response_format': {'type': 'json_schema', 'json_schema': {'schema': {'type': 'object'}}}})
        self.assertEqual(translated['messages'][1]['content'][0]['input'], {'q': 'show'})
        self.assertEqual(translated['messages'][2]['content'][0]['type'], 'tool_result')
        self.assertIn('schema', translated['system'])

    def test_search_title_does_not_manufacture_tmdb_match(self):
        subs = [{'title': 'Luke Cage', 'tmdbid': 62126, 'auto': True}]
        self.assertEqual(matching({'title': 'Other show'}, subs), [])

    def test_upstream_share_parser_contract(self):
        from reelay.core.parse_rules import parse_115_url
        url, code, password = parse_115_url('https://115cdn.com/s/abc123?password=abcd', [])
        self.assertEqual(code, 'abc123')
        self.assertEqual(password, 'abcd')
        self.assertTrue(url.startswith('https://'))

    def test_existing_fnos_single_level_categories(self):
        from reelay.plugins.organizer.category import validate_strategy, classify
        strategy = {'tv': {'日剧': {'origin_country': 'JP'}, '电视剧': None}}
        validate_strategy(strategy)
        self.assertEqual(classify(strategy, 'tv', [], ['JP']), '日剧')
        for path in ('../escape', '/absolute', 'a/b/c', 'a\\b'):
            with self.assertRaises(ValueError):
                validate_strategy({'tv': {path: None}})


if __name__ == '__main__':
    unittest.main()
