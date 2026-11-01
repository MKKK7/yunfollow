"""Constrained agent API: proposal credentials cannot confirm writes."""
import os
import secrets
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .intake import Intake
from .catalog import Catalog
from .timeline import summary


def authorized(value, expected):
    return bool(expected and len(expected) >= 24 and secrets.compare_digest(value, 'Bearer ' + expected))


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=1000)
    preferences: dict | None = None


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    tmdbid: int = Field(gt=0)
    season: int = Field(gt=0, le=1000)


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    url: str = Field(max_length=500)
    name: str = Field(default='', max_length=80)
    note: str = Field(default='', max_length=500)
    interval_seconds: int = Field(default=600, ge=60, le=86400)


class SyncStatus(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str


class OfflineInput(BaseModel):
    uri: str = Field(min_length=10, max_length=4096)
    subscription_id: int = Field(gt=0)


def router(service, prefix='/fnos/v1'):
    route = APIRouter(prefix=prefix, tags=['YunFollow workflow'])
    intake = Intake(service)

    def proposal(request: Request):
        if not authorized(request.headers.get('authorization', ''), os.getenv('FNOS_PROPOSAL_KEY')):
            raise HTTPException(401, 'Proposal credential required')

    def owner(request: Request):
        from ..core import secret_store
        expected = os.getenv('FNOS_OWNER_KEY')
        if expected in (os.getenv('FNOS_PROPOSAL_KEY'), secret_store.get('fnos_source_key')) or not authorized(request.headers.get('authorization', ''), expected):
            raise HTTPException(401, 'Separate owner credential required; never give it to an AI agent')

    def source_reader(request: Request):
        from ..core import secret_store
        token = request.headers.get('authorization', '')
        if not any(authorized(token, key) for key in (os.getenv('FNOS_PROPOSAL_KEY'), os.getenv('FNOS_OWNER_KEY'), secret_store.get('fnos_source_key'))):
            raise HTTPException(401, 'Source read credential required')

    def source_bridge(request: Request):
        from ..core import secret_store
        if not authorized(request.headers.get('authorization', ''), secret_store.get('fnos_source_key')):
            raise HTTPException(401, 'Dedicated document bridge credential required')

    @route.get('/sources', dependencies=[Depends(source_reader)])
    async def sources():
        return Catalog(service.store).public()

    @route.post('/sources', dependencies=[Depends(owner)])
    async def add_source(body: Source):
        return Catalog(service.store).add(**body.model_dump())

    @route.delete('/sources/{source_id}', dependencies=[Depends(owner)])
    async def remove_source(source_id: str):
        Catalog(service.store).remove(source_id)
        return {'removed': True}

    @route.post('/sources/{source_id}/snapshot', dependencies=[Depends(source_bridge)])
    async def document_snapshot(source_id: str, request: Request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 2 * 1024 * 1024:
                raise HTTPException(413, 'Candidate snapshot exceeds2MiB')
            chunks.append(chunk)
        try:
            value = json.loads(b''.join(chunks))
        except (ValueError, UnicodeError):
            raise HTTPException(422, 'Invalid snapshot JSON') from None
        return Catalog(service.store).snapshot(source_id, value)

    @route.post('/search', dependencies=[Depends(owner)])
    async def trigger_search():
        from .management import wake
        wake(service)
        return {'state': 'scheduled'}

    @route.post('/sources/{source_id}/sync-status', dependencies=[Depends(source_bridge)])
    async def sync_status(source_id: str, body: SyncStatus):
        Catalog(service.store).sync_status(source_id, body.state)
        return {'recorded': True}

    @route.get('/audit', dependencies=[Depends(proposal)])
    async def audit():
        return list(service.store.state_get('fnos_preflight', 'audit', {}).values())

    @route.get('/integrations', dependencies=[Depends(proposal)])
    async def integrations():
        from .integrations import latest
        return latest(service.store)

    @route.post('/integrations/check', dependencies=[Depends(owner)])
    async def check_integrations():
        from .integrations import check
        return await check(service)

    @route.get('/status', dependencies=[Depends(proposal)])
    async def status():
        from ..core import secret_store
        model = service.pipeline.cfg.get('llm', {}).get('model', '') if hasattr(service.pipeline, 'cfg') else ''
        return {'mode': 'cloud_only', 'defaults': {'resolution': '1080p', 'subtitles': 'zh', 'overwrite': False},
                'sources': Catalog(service.store).public(),
                'search': service.store.state_get('fnos_sources', 'progress', {}),
                'configured': {'model': bool(secret_store.get('llm_api_key') and model and not model.startswith('SET_')),
                               'tmdb': bool(secret_store.get('tmdb_api_key')),
                               'telegram': bool(secret_store.get('control_bot_token') and os.getenv('FNOS_TELEGRAM_OWNER_ID'))},
                'subscriptions': service.store.subscriptions()}

    @route.post('/drafts', dependencies=[Depends(proposal)])
    async def preview(body: Preview):
        return await intake.preview(body.text, 'api-owner', body.preferences)

    @route.post('/chat', dependencies=[Depends(proposal)])
    async def chat(body: Preview):
        from .conversation import answer
        return await answer(service, body.text)

    @route.post('/drafts/{draft_id}/confirm', dependencies=[Depends(owner)])
    async def confirm(draft_id: str, body: Confirm):
        return await intake.confirm(draft_id, 'api-owner', body.tmdbid, body.season)

    @route.get('/subscriptions', dependencies=[Depends(proposal)])
    async def subscriptions():
        return service.store.subscriptions()

    @route.get('/jobs', dependencies=[Depends(proposal)])
    async def jobs():
        return [summary(ctx) for row in service.list_tasks(limit=50) if (ctx := service.store.get_task(row['task_id'])) and ctx.raw.extra.get('fnos_preflight')]

    @route.get('/jobs/{task_id}', dependencies=[Depends(proposal)])
    async def job(task_id: str):
        ctx = service.store.get_task(task_id)
        if not ctx or not ctx.raw.extra.get('fnos_preflight'):
            raise HTTPException(404, 'Qualified job not found')
        return summary(ctx)

    @route.get('/candidates', dependencies=[Depends(proposal)])
    async def candidates():
        from .candidates import batch
        return batch(service.store)

    @route.post('/candidates/{identity}/select', dependencies=[Depends(owner)])
    async def select_candidate(identity: str):
        from .candidates import select
        return await select(service, identity)

    @route.post('/batches/{identity}/next', dependencies=[Depends(owner)])
    async def next_candidates(identity: str):
        from .candidates import next_batch
        return next_batch(service.store, identity)

    @route.post('/offline', dependencies=[Depends(proposal)])
    async def offline_draft(body: OfflineInput):
        from .offline import create
        return create(service, body.uri, body.subscription_id)

    @route.get('/offline', dependencies=[Depends(proposal)])
    async def offline_jobs():
        from .offline import jobs
        return jobs(service.store)

    @route.post('/offline/{identity}/confirm', dependencies=[Depends(owner)])
    async def offline_confirm(identity: str):
        from .offline import confirm
        return confirm(service, identity)

    return route
