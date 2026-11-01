import contextlib
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reelay.core.contracts import ParsedResource, RawMessage
from reelay.core.store import Store
from reelay.fnos import candidates
from reelay.fnos.catalog import Catalog
from reelay.fnos.classification import category
from reelay.fnos.console import router
from reelay.fnos.intent import interpret
from reelay.fnos.management import refresh_airing, subscriptions
from reelay.fnos.verification import existing_connection


class YunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.target = {'title': 'Test Show', 'tmdbid': 123, 'season': 1, 'media_type': 'tv'}
        self.store.add_subscription('tmdb:123:S1', {**self.target, 'auto': True, 'total': 12})

    def candidate(self, number):
        raw = RawMessage(source='@fnos_pansou', msg_id=str(number), text='Test Show', extra={
            'fnos_subscription': self.target, 'fnos_file_evidence': {'files': [{'name': 'Test.S01E09.mkv', 'size': 500}], 'pairs': [[1, 9]]}})
        return candidates.record(self.store, raw, {'title': 'Test Show', 'share': {'shareCode': 'example' + str(number)}},
            {'id': 'source1', 'kind': 'pansou', 'name': 'My source'}, self.target,
            {'file_hash': 'hash' + str(number), 'parsed': {'episodes': [9], 'year': 2024, 'media_type': 'tv'}})

    def test_three_choices_and_next_only_ignores_the_displayed_batch(self):
        ids = [self.candidate(n) for n in range(5)]
        first = candidates.batch(self.store)
        self.assertEqual(len(first['items']), 3)
        second = candidates.next_batch(self.store, first['id'])
        self.assertEqual(len(second['items']), 2)
        self.assertFalse({v['id'] for v in first['items']} & {v['id'] for v in second['items']})
        self.assertEqual(sum(self.store.state_get('yun_candidates', key)['state'] == 'ignored' for key in ids), 3)
        self.assertEqual(self.store.list_tasks(), [])

    async def test_selected_candidate_is_rechecked_and_only_accepted_once(self):
        identity = self.candidate(1)
        accept = AsyncMock(return_value=NS(task_id='a' * 12))
        service = NS(store=self.store, pipeline=NS(accept=accept))
        with patch('reelay.fnos.candidates.Preflight') as preflight:
            preflight.return_value.check = AsyncMock(return_value=('accepted', 'ready'))
            first = await candidates.select(service, identity)
            second = await candidates.select(service, identity)
        self.assertEqual(first['task_id'], second['task_id'])
        accept.assert_awaited_once()
        self.assertTrue(self.store.state_get('yun_follow', '123:1')['auto_missing'])

    async def test_share_expiring_after_card_is_not_transferred(self):
        identity = self.candidate(1)
        service = NS(store=self.store, pipeline=NS(accept=AsyncMock()))
        with patch('reelay.fnos.candidates.Preflight') as preflight:
            preflight.return_value.check = AsyncMock(return_value=('excluded', 'share_unavailable'))
            with self.assertRaises(ValueError):
                await candidates.select(service, identity)
        service.pipeline.accept.assert_not_awaited()
        self.assertEqual(candidates.available(self.store), [])

    def test_source_metadata_edits_persist_and_cannot_inject_credentials(self):
        catalog = Catalog(self.store)
        source = catalog.add('telegram', 'https://t.me/examplechannel', name='My channel', note='Animation')
        catalog.update(source['id'], {'enabled': False, 'name': 'Archive', 'interval_seconds': 120})
        value = catalog.public()[0]
        self.assertFalse(value['enabled'])
        self.assertEqual(value['name'], 'Archive')
        self.assertEqual(value['note'], 'Animation')
        with self.assertRaises(ValueError):
            catalog.update(source['id'], {'api_key': 'secret'})

    def test_movie_and_anime_priority_and_unknown_category(self):
        self.assertEqual(category(ParsedResource(media_type='tv', genre_ids=[16], origin_country=['JP'])), '动漫')
        self.assertEqual(category(ParsedResource(media_type='tv', genre_ids=[16], origin_country=['US'])), '动漫')
        self.assertEqual(category(ParsedResource(media_type='movie', genre_ids=[16], origin_country=['JP'])), '电影')
        self.assertEqual(category(ParsedResource(media_type='tv', genre_ids=[18], origin_country=['JP'])), '日剧')
        self.assertIsNone(category(ParsedResource(media_type='tv', origin_country=[])))

    async def test_future_episode_is_not_reported_missing(self):
        service = NS(store=self.store, pipeline=NS(scope=lambda: contextlib.nullcontext()))
        with patch('reelay.fnos.management.tmdb.season_episodes', return_value=[
            {'episode': 1, 'air_date': '2020-01-01'}, {'episode': 2, 'air_date': '2099-01-01'}]):
            await refresh_airing(service)
        value = subscriptions(service)[0]
        self.assertEqual(value['missing'], [1])
        self.assertEqual(value['airing']['unreleased'], [2])

    async def test_plain_language_next_batch_is_deterministic(self):
        self.assertEqual(await interpret(None, '下一批'), '/next')
        self.assertEqual(await interpret(None, '选择第二个'), '/choose 2')

    def test_masked_key_does_not_become_a_credential_or_read_other_databases(self):
        snapshot = {'id': 'connection1', 'emby_url': 'http://emby.test', 'api_key': 'ab****cd'}
        with patch.dict('os.environ', {'YUNFOLLOW_EMBY_READ_KEY': ''}):
            self.assertIsNone(existing_connection(snapshot))
        with patch.dict('os.environ', {'YUNFOLLOW_EMBY_READ_KEY': 'test-only-explicit-key'}):
            self.assertEqual(existing_connection(snapshot)['api_key'], 'test-only-explicit-key')
    def test_console_source_edit_and_candidate_list_are_operational(self):
        service = NS(store=self.store)
        app = FastAPI()
        app.include_router(router(service))
        client = TestClient(app)
        response = client.post('/api/yunfollow/sources', json={'kind': 'telegram', 'url': 'https://t.me/examplechannel', 'name': 'My channel'})
        self.assertEqual(response.status_code, 200)
        identity = response.json()['id']
        self.assertEqual(client.patch('/api/yunfollow/sources/' + identity, json={'note': 'News', 'enabled': False}).status_code, 200)
        self.assertFalse(client.get('/api/yunfollow/sources').json()[0]['enabled'])
        self.candidate(1)
        value = client.get('/api/yunfollow/candidates').json()['items'][0]
        self.assertEqual(value['files'][0]['size'], 500)
        self.assertEqual(value['new_episodes'], [9])
