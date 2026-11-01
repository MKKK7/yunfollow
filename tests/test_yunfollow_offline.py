import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from reelay.core.store import Store
from reelay.fnos.offline import confirm, create, identity, ingest, jobs, tick


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        sub = self.store.add_subscription('tmdb:123:S1', {'title': 'Test Show', 'tmdbid': 123, 'season': 1, 'auto': True})
        self.sub = sub['id']
        self.uri = 'magnet:?xt=urn:btih:' + 'a' * 40

    def test_links_are_validated_and_only_115_offline_schemes_are_allowed(self):
        self.assertEqual(identity(self.uri), 'a' * 40)
        for uri in ['https://pan.quark.cn/s/example', 'https://example.test/video.mp4', 'magnet:?xt=invalid']:
            with self.assertRaises(ValueError):
                identity(uri)

    def test_draft_has_no_cloud_side_effect_and_repeated_confirm_does_not_requeue(self):
        service = NS(store=self.store)
        value = create(service, self.uri, self.sub)
        self.assertEqual(value['state'], 'waiting_confirmation')
        self.assertNotIn('uri', jobs(self.store)[0])
        confirm(service, value['id'])
        saved = self.store.state_get('yun_offline', value['id'])
        saved['state'] = 'submission_unknown'
        self.store.state_set('yun_offline', value['id'], saved)
        self.assertEqual(confirm(service, value['id'])['state'], 'submission_unknown')
        self.assertEqual(self.store.list_tasks(), [])

    async def test_uncertain_submission_is_never_resubmitted(self):
        value = create(NS(store=self.store), self.uri, self.sub)
        confirm(NS(store=self.store), value['id'])
        value = self.store.state_get('yun_offline', value['id'])
        client = NS(clouddownload_task_add_url=Mock(side_effect=TimeoutError), clouddownload_task_list=Mock(return_value={'tasks': []}))
        drive = NS(iterdir=Mock(return_value=[]), mkdir=Mock(return_value=321))
        storage = NS(library=lambda: drive, _cookie_client=lambda: client)
        service = NS(store=self.store, pipeline=NS(reload_config=lambda: None, reg=NS(get_provider=lambda *a: storage), cfg={'save_dirs': {'root': '10'}, 'defaults': {'save_to': 'root'}}))
        await tick(service, value)
        self.assertEqual(value['state'], 'submission_unknown')
        await tick(service, value)
        client.clouddownload_task_add_url.assert_called_once()
        self.assertEqual(value['state'], 'submission_unknown')

    async def test_ingest_cannot_run_before_download_and_file_confirmation(self):
        value = create(NS(store=self.store), self.uri, self.sub)
        service = NS(store=self.store, pipeline=NS(accept=AsyncMock()))
        with self.assertRaises(ValueError):
            await ingest(service, value['id'])
        self.assertEqual(self.store.list_tasks(), [])
