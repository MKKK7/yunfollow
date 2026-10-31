import unittest
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

from reelay.core.store import Store
from reelay.fnos.catalog import Catalog
from reelay.fnos.discovery import discover, episode_facts, results, share_identity
from reelay.fnos.sources import FnosSources
from tests.test_fnos_sources import FakePipeline


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_episode_41_is_regular_but_42_trailer_and_movies_are_not(self):
        files = [{'name': name} for name in ['41_4K_HDR.mp4', '第41话 开始学习吧小黑同学.mp4',
                 '第42话 预告.mp4', '罗小黑战记 (2019).mp4']]
        self.assertEqual([x['episode'] for x in episode_facts(files)], [41, 41])

    def test_share_urls_reject_untrusted_hosts(self):
        for url in ['https://evil.test/s/abc', 'http://pan.quark.cn/s/abc', 'https://pan.quark.cn@evil.test/s/abc']:
            with self.assertRaises(ValueError):
                share_identity('quark', url)

    async def test_external_scan_records_real_files_and_never_emits_transfer(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.add_subscription('tmdb:123:S1', {'title': 'Test Show', 'tmdbid': 123, 'season': 1, 'auto': True})
        Catalog(store).add('pansou', 'http://pansou.test:8085')
        candidate = {'title': 'Test Show', 'share': {'cloud': 'quark', 'url': 'https://pan.quark.cn/s/example'}, 'version': '1'}
        source = FnosSources()
        source.bind_pipeline(FakePipeline(store, {}))
        source.collect = AsyncMock(return_value={'1': candidate})
        read = AsyncMock(return_value=[{'name': '41_4K_HDR.mp4', 'episode': 41, 'season': None}])
        async def verify(store, candidate, target):
            return await discover(store, candidate, target, read=read)
        emit = AsyncMock()
        with tempfile.TemporaryDirectory() as folder, patch('reelay.fnos.sources.paths.DATA', Path(folder)), patch('reelay.fnos.sources.discover', side_effect=verify):
            await source.scan(emit)
        emit.assert_not_awaited()
        self.assertEqual(results(store)[0]['episodes'], [41])
        self.assertFalse(results(store)[0]['video_downloaded'])
        self.assertEqual(store.state_get('fnos_sources', 'progress')['external_verified'], 1)

    async def test_pansou_search_does_not_restrict_discovery_to_115(self):
        source = FnosSources()
        response = {'data': {'merged_by_type': {'quark': [{'title': 'Test Show 更新41', 'url': 'https://pan.quark.cn/s/example'}]}}}
        with patch('reelay.fnos.sources.api', new_callable=AsyncMock, return_value=response) as request:
            items = await source.collect({'kind': 'pansou', 'url': 'http://pansou.test'}, [{'title': 'Test Show'}], None, None, lambda *a, **k: None)
        self.assertEqual(len(items), 1)
        self.assertNotIn('cloud_types', request.call_args.args[1])
