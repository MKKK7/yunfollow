import unittest
from unittest.mock import AsyncMock, patch

from reelay.fnos.sources import FnosSources


class Only115Tests(unittest.IsolatedAsyncioTestCase):
    async def test_search_ignores_other_clouds_even_if_pansou_returns_them(self):
        response = {'data': {'merged_by_type': {'quark': [{'title': 'Test Show', 'url': 'https://pan.quark.cn/s/example'}],
                    'aliyun': [{'title': 'Test Show', 'url': 'https://www.alipan.com/s/example'}]}}}
        with patch('reelay.fnos.sources.api', new_callable=AsyncMock, return_value=response) as request:
            items = await FnosSources().collect({'kind': 'pansou', 'url': 'http://pansou.test'}, [{'title': 'Test Show'}], None, None, lambda *a, **k: None)
        self.assertEqual(items, {})
        self.assertEqual(request.call_args.args[1]['cloud_types'], ['115', 'magnet', 'ed2k'])
