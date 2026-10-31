"""Refresh registered documents using the official CLI inside this container."""
import asyncio
import time

from ..core import paths
from .catalog import Catalog


async def refresh_documents(store, read=None):
    from .sources import command_json
    reader = read or command_json
    catalog = Catalog(store)
    results = []
    for source in catalog.items():
        if source['kind'] != 'kdocs' or not source['enabled']:
            continue
        try:
            value = await reader(['node', '/app/fnos-adapters/kdocs-container.mjs',
                                  '--url', source['url'], '--cli', '/usr/local/bin/kdocs-cli'])
            receipt = catalog.snapshot(source['id'], value)
            catalog.health(source['id'], 'ok', sync_mode='container_cli', sync_state='ok',
                           count=receipt['candidates'], last_sync_attempt_at=time.time())
            results.append({'source_id': source['id'], 'state': 'ok', 'candidates': receipt['candidates']})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            catalog.sync_status(source['id'], 'sync_error')
            catalog.health(source['id'], 'sync_error', sync_mode='container_cli', error=type(exc).__name__)
            results.append({'source_id': source['id'], 'state': 'sync_error'})
    store.state_set('fnos_kdocs', 'last', {'updated_at': time.time(), 'mode': 'container_cli', 'results': results})
    return results


async def run():
    from ..core.store import Store
    store = Store(str(paths.DATABASE))
    try:
        while True:
            await refresh_documents(store)
            deadline = time.monotonic() + 900
            fingerprint = [(s['id'], s['enabled']) for s in Catalog(store).items() if s['kind'] == 'kdocs']
            while time.monotonic() < deadline:
                await asyncio.sleep(2)
                now = [(s['id'], s['enabled']) for s in Catalog(store).items() if s['kind'] == 'kdocs']
                if now != fingerprint:
                    break
    finally:
        store.close()
