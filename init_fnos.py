"""Initialize only a fresh deployment; never overwrite an existing configuration."""
import json
import os
from reelay.core import paths
from reelay.core.config import normalize_config, write_config
from reelay.core.registry import discover

if paths.CONFIG.exists():
    raise SystemExit('Configuration exists; left unchanged')

save_cid = os.getenv('FNOS_SAVE_CID', '')
if not save_cid.isdecimal() or int(save_cid) == 0:
    raise SystemExit('Set FNOS_SAVE_CID to an explicitly selected115 library root')

cfg = normalize_config({
    'save_dirs': {'默认': save_cid},
    'providers': {'source': 'fnos_sources', 'notify': ['console', 'fnos_bot']},
    'defaults': {'approval_mode': 'manual', 'parser': 'fnos_verified', 'decision': 'fnos_subscription',
                 'storage': 'p115', 'save_to': '默认'},
    'sources': [{'bot': ['@fnos_public', '@fnos_document', '@fnos_pansou'], 'save_to': '默认'}],
    'llm': {'base_url': 'https://api.minimaxi.com/anthropic', 'model': 'SET_YOUR_MINIMAX_MODEL'},
    'runtime': {'concurrency': 1},
    'plugins': {
        'organizer': {'conflict': 'manual', 'layout': 'category'},
        'fnos_sources': {'channels': [], 'poll_seconds': 600},
        'fnos_media': {'enabled': True, 'strm_task_id': int(os.getenv('LITEPAN_STRM_TASK_ID', '0')),
                       'emby_config_id': os.getenv('LITEPAN_EMBY_CONFIG_ID', ''),
                       'emby_libraries': json.loads(os.getenv('LITEPAN_EMBY_LIBRARIES', '{}'))},
    },
})
discover().validate_config(cfg)
write_config(paths.CONFIG, cfg)
print('Fresh configuration initialized; no subscriptions created')
