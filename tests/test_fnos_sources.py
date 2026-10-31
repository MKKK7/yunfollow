import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from reelay.core import secret_store
from reelay.core.contracts import Decision, ParsedResource, PipelineContext, RawMessage, TaskState
from reelay.core.store import Store
from reelay.fnos.api import router
from reelay.fnos.catalog import Catalog, normalize
from reelay.fnos.preflight import Preflight, VerifiedParser
from reelay.fnos.timeline import summary
from reelay.fnos.sources import FnosSources
from reelay.fnos.media import FnosMedia


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.catalog = Catalog(self.store)

    def tearDown(self):
        self.store.close()

    def test_owner_can_add_and_remove_sources_without_readding_defaults(self):
        first = self.catalog.add('telegram', 'https://t.me/s/ExampleChannel')
        second = self.catalog.add('telegram', 'https://t.me/examplechannel')
        self.assertEqual(first['id'], second['id'])
        self.catalog.remove(first['id'])
        self.catalog.seed({'plugins': {'fnos_sources': {'channels': ['ExampleOther']}}})
        self.assertEqual(self.catalog.items(), [])

    def test_no_private_channels_credentials_or_arbitrary_document_urls(self):
        for kind, url in [('telegram', 'https://t.me/+private'), ('telegram', 'https://evil.test/channel'),
                          ('pansou', 'http://user:password@localhost:8085'), ('kdocs', 'https://www.kdocs.cn/latest')]:
            with self.assertRaises(ValueError):
                normalize(kind, url)

    def test_document_snapshots_are_bound_to_source_and_store_no_raw_body(self):
        item = self.catalog.add('kdocs', 'https://www.kdocs.cn/l/ExampleDoc')
        snapshot = {'source': {'url': item['url']}, 'status': 'ok', 'contentPersisted': False,
                    'content': 'RAW_BODY_MUST_NOT_PERSIST', 'completeness': {},
                    'candidates': {'shares115': [{'shareCode': 'example123', 'password': 'abcd', 'contextSnippet': 'title'}]}}
        self.catalog.snapshot(item['id'], snapshot)
        saved = self.store.state_get('fnos_documents', item['id'])
        self.assertNotIn('RAW_BODY_MUST_NOT_PERSIST', json.dumps(saved))
        self.assertEqual(self.catalog.public()[0]['health']['state'], 'ok')
        snapshot['source']['url'] = 'https://www.kdocs.cn/l/OtherDoc'
        with self.assertRaises(ValueError):
            self.catalog.snapshot(item['id'], snapshot)

    def test_stale_or_failed_document_sync_is_not_reported_successful(self):
        item = self.catalog.add('kdocs', 'https://www.kdocs.cn/l/ExampleDoc')
        self.assertEqual(self.catalog.public()[0]['health']['state'], 'needs_sync')
        self.catalog.sync_status(item['id'], 'auth_error')
        self.assertEqual(self.catalog.public()[0]['health']['state'], 'auth_error')


class FakePipeline:
    def __init__(self, store, facts, identity=None):
        self.store, self.cfg = store, {}
        self.storage = NS(inspect_share=lambda *a, **k: facts, save=AsyncMock(), prepare=AsyncMock())
        self.parser = NS(parse=AsyncMock(return_value=ParsedResource(is_media=True, confidence=1, media_type='tv', title='Test Show', season=1, episodes=[1], resolution='1080p')))
        self.decider = NS(decide=AsyncMock(return_value=Decision.ASK))
        self.identity = identity or {'matched': True, 'kind': 'tv', 'tmdbid': 123}
        self.reg = NS(get_provider=lambda kind, name: {'storage': self.storage, 'parser': self.parser, 'decision': self.decider}[kind])
        self.facts = facts

    def reload_config(self):
        pass

    def scope(self, *args):
        return contextlib.nullcontext()

    def rule_for(self, source):
        return {'storage': 'p115'}

    def stamp_config(self, ctx):
        pass

    async def run_hooks(self, stage, ctx):
        ctx.meta['tmdb'] = self.identity
        ctx.meta['share'] = self.facts


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(':memory:')
        self.candidate = {'title': 'Test Show', 'share': {'shareCode': 'example123', 'password': 'abcd'}}
        self.target = {'title': 'Test Show', 'tmdbid': 123, 'season': 1}
        self.raw = RawMessage(source='@fnos_pansou', msg_id='1', text='Test Show https://115.com/s/example123', extra={'fnos_subscription': self.target})

    async def asyncTearDown(self):
        self.store.close()

    async def test_expired_share_is_excluded_before_model_and_before_task_creation(self):
        pipe = FakePipeline(self.store, {'status': 'unavailable'})
        result = await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target)
        self.assertEqual(result, ('excluded', 'share_unavailable'))
        pipe.parser.parse.assert_not_awaited()
        pipe.storage.save.assert_not_awaited()

    async def test_litepan_failure_marks_the_real_stage_failed_without_calling_later_stages(self):
        ctx = PipelineContext(raw=self.raw, meta={'organize':{'applied':True}})
        store = NS(save_task=lambda ctx: None)
        with patch('reelay.fnos.timeline.runtime.services', return_value=NS(store=store)), patch('reelay.fnos.media.LitePan.login', new_callable=AsyncMock, side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                await FnosMedia().run(ctx)
        stages = ctx.meta['fnos_workflow']['stages']
        self.assertEqual(stages['strm']['state'], 'failed')
        self.assertNotIn('emby', stages)
        self.assertEqual(self.store.list_tasks(), [])
        self.assertNotIn('abcd', json.dumps(self.store.state_get('fnos_preflight', 'audit')))

    async def test_real_scan_does_not_emit_a_job_for_an_expired_search_result(self):
        self.store.add_subscription('tmdb:123:S1', {'title':'Test Show','tmdbid':123,'season':1,'auto':True})
        Catalog(self.store).add('pansou', 'http://pansou.test:8085')
        pipe = FakePipeline(self.store, {'status':'unavailable'})
        source = FnosSources()
        source.bind_pipeline(pipe)
        source.collect = AsyncMock(return_value={'example123': {**self.candidate, 'source':'@fnos_pansou','version':'1'}})
        emit = AsyncMock()
        with tempfile.TemporaryDirectory() as folder, patch('reelay.fnos.sources.paths.DATA', Path(folder)):
            await source.scan(emit)
        emit.assert_not_awaited()
        progress = self.store.state_get('fnos_sources','progress')
        self.assertEqual(progress['excluded'], 1)
        self.assertEqual(progress['emitted'], 0)

    async def test_two_movie_files_do_not_enter_the_television_queue(self):
        pipe = FakePipeline(self.store, {'status': 'issue', 'file_evidence': [{'name': 'Test Show (2019).mkv'}, {'name': 'Test Show (2019) 1080p.mp4'}]})
        self.assertEqual(await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target), ('excluded', 'not_episodic_files'))
        pipe.parser.parse.assert_not_awaited()
        self.assertEqual(self.store.list_tasks(), [])

    async def test_network_failure_is_deferred_and_never_converted_to_dead_link(self):
        pipe = FakePipeline(self.store, {})
        def fail(*args, **kwargs):
            raise TimeoutError()
        pipe.storage.inspect_share = fail
        verdict, reason = await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target)
        self.assertEqual(verdict, 'deferred')
        self.assertIn('TimeoutError', reason)
        self.assertEqual(self.store.list_tasks(), [])

    async def test_115_authentication_failure_is_not_misreported_as_expired_resource(self):
        pipe = FakePipeline(self.store, {'status':'unavailable','note':'请重新登录'})
        self.assertEqual(await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target), ('deferred','115_auth_required'))

    async def test_wrong_tmdb_identity_is_excluded_even_for_same_title(self):
        pipe = FakePipeline(self.store, {'status': 'ok'}, {'matched': True, 'kind': 'movie', 'tmdbid': 999})
        with patch('reelay.fnos.preflight.confidence.apply'):
            self.assertEqual(await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target), ('excluded', 'movie'))

    async def test_validated_result_can_be_reused_but_cannot_be_forged_by_raw_message(self):
        pipe = FakePipeline(self.store, {'status': 'ok', 'pairs': [[1, 1]], 'file_evidence': [{'name': 'Test.Show.S01E01.mkv'}]})
        with patch('reelay.fnos.preflight.confidence.apply'):
            self.assertEqual(await Preflight(pipe).check(self.raw, self.candidate, 'source1', self.target), ('accepted', 'quality_review'))
        ctx = PipelineContext(raw=self.raw)
        with patch('reelay.fnos.preflight.runtime.services', return_value=NS(store=self.store)):
            parsed = await VerifiedParser().parse(ctx)
            self.assertEqual(parsed.title, 'Test Show')
            self.assertEqual(ctx.meta['fnos_workflow']['preflight']['state'], 'done')
            self.raw.text = 'tampered source'
            with patch('reelay.providers.parser.LLMParser.parse', new_callable=AsyncMock, return_value=None) as fallback:
                await VerifiedParser().parse(PipelineContext(raw=self.raw))
                fallback.assert_awaited_once()
        pipe.storage.save.assert_not_awaited()

    async def test_share_is_rechecked_and_new_episode_creates_a_new_immutable_receipt(self):
        facts = {'status':'ok','pairs':[[1,1]],'file_evidence':[{'name':'Test.Show.S01E01.mkv'}]}
        pipe = FakePipeline(self.store, facts)
        reader = Preflight(pipe)
        with patch('reelay.fnos.preflight.confidence.apply'):
            await reader.check(self.raw, self.candidate, 'source1', self.target)
            first_key = self.raw.extra['fnos_preflight']
            await reader.check(self.raw, self.candidate, 'source1', self.target)
            self.assertEqual(pipe.parser.parse.await_count, 1)
            facts['pairs'].append([1,2])
            facts['file_evidence'].append({'name':'Test.Show.S01E02.mkv'})
            await reader.check(self.raw, self.candidate, 'source1', self.target)
            self.assertNotEqual(first_key, self.raw.extra['fnos_preflight'])
            self.assertEqual(pipe.parser.parse.await_count, 2)
            pipe.storage.inspect_share = lambda *a, **k: {'status':'unavailable'}
            self.assertEqual(await reader.check(self.raw, self.candidate, 'source1', self.target), ('excluded','share_unavailable'))


class BridgeApiTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.service = NS(store=self.store)
        app = FastAPI()
        app.include_router(router(self.service))
        @app.exception_handler(ValueError)
        async def invalid(request, exc):
            return JSONResponse({'detail': str(exc)}, status_code=422)
        self.client = TestClient(app)
        self.env = patch.dict(os.environ, {'FNOS_OWNER_KEY': 'o' * 32, 'FNOS_PROPOSAL_KEY': 'p' * 32})
        self.env.start()
        self.previous = secret_store.current()
        secret_store.use(secret_store.SecretStore(None, {'fnos_source_key': 's' * 32}))

    def tearDown(self):
        secret_store.use(self.previous)
        self.env.stop()
        self.store.close()

    def test_document_bridge_can_sync_but_cannot_add_sources_or_confirm_subscriptions(self):
        headers = {'Authorization': 'Bearer ' + 's' * 32}
        self.assertEqual(self.client.get('/fnos/v1/sources', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/fnos/v1/sources', headers=headers, json={'kind':'telegram','url':'https://t.me/examplechannel'}).status_code, 401)
        self.assertEqual(self.client.post('/fnos/v1/drafts/nope/confirm', headers=headers, json={'tmdbid':123,'season':1}).status_code, 401)
        item = Catalog(self.store).add('kdocs', 'https://www.kdocs.cn/l/ExampleDoc')
        snapshot = {'source':{'url':item['url']},'status':'ok','contentPersisted':False,'candidates':{'shares115':[]}}
        self.assertEqual(self.client.post('/fnos/v1/sources/'+item['id']+'/snapshot', headers=headers, json=snapshot).status_code, 200)


class TimelineTests(unittest.TestCase):
    def test_configured_services_do_not_fabricate_completed_stages(self):
        ctx = PipelineContext(raw=RawMessage(source='@fnos_public', msg_id='1', text='Test'))
        view = summary(ctx)
        self.assertEqual(next(s for s in view['stages'] if s['id']=='emby')['state'], 'pending')
        ctx.state = TaskState.SAVING
        self.assertEqual(next(s for s in summary(ctx)['stages'] if s['id']=='transfer')['state'], 'running')

    def test_only_real_storage_receipt_completes_transfer_stage(self):
        ctx = PipelineContext(raw=RawMessage(source='@fnos_public', msg_id='1', text='Test'), meta={'storage':{'phase':'stored'},'organize':{'applied':True}})
        states = {s['id']:s['state'] for s in summary(ctx)['stages']}
        self.assertEqual(states['transfer'], 'done')
        self.assertEqual(states['emby'], 'pending')


class ContainerDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_sync_reads_registered_document_and_persists_candidates_only(self):
        from reelay.fnos.kdocs import refresh_documents
        store = Store(':memory:')
        catalog = Catalog(store)
        item = catalog.add('kdocs', 'https://www.kdocs.cn/l/ExampleDoc')
        reader = AsyncMock(return_value={'source':{'url':item['url']},'status':'ok','contentPersisted':False,
            'content':'must not persist','candidates':{'shares115':[{'shareCode':'abc123','contextSnippet':'title'}]}})
        result = await refresh_documents(store, read=reader)
        self.assertEqual(result[0]['candidates'], 1)
        self.assertEqual(catalog.public()[0]['health']['sync_mode'], 'container_cli')
        self.assertNotIn('must not persist', json.dumps(store.state_get('fnos_documents',item['id'])))
        self.assertEqual(reader.call_args.args[0][-1], '/usr/local/bin/kdocs-cli')
        store.close()

    async def test_failed_container_sync_preserves_previous_snapshot_but_reports_error(self):
        from reelay.fnos.kdocs import refresh_documents
        store = Store(':memory:')
        catalog = Catalog(store)
        item = catalog.add('kdocs', 'https://www.kdocs.cn/l/ExampleDoc')
        catalog.snapshot(item['id'], {'source':{'url':item['url']},'status':'ok','contentPersisted':False,'candidates':{'shares115':[]}})
        await refresh_documents(store, read=AsyncMock(side_effect=RuntimeError('offline')))
        self.assertEqual(catalog.public()[0]['health']['state'], 'sync_error')
        self.assertIsNotNone(store.state_get('fnos_documents',item['id']))
        store.close()
