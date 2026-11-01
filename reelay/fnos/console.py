"""Console endpoints use the upstream same-origin login and CSRF guard."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from . import candidates, integrations, management
from .catalog import Catalog
from .classification import CATEGORIES, choose
from .intake import Intake
from .timeline import summary


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str
    url: str = Field(max_length=500)
    name: str = Field(default='', max_length=80)
    note: str = Field(default='', max_length=500)
    interval_seconds: int = Field(default=600, ge=60, le=86400)


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str
    destination: str | None = None


class IntakeText(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    preferences: dict | None = None


class Selection(BaseModel):
    tmdbid: int = Field(gt=0)
    season: int = Field(gt=0)


class OfflineInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    uri: str = Field(min_length=10, max_length=4096)
    subscription_id: int = Field(gt=0)


class PreferencesInput(BaseModel):
    preferences: dict
    auto_missing: bool


class NamingInput(BaseModel):
    movie: str = Field(min_length=1, max_length=1000)
    tv: str = Field(min_length=1, max_length=1000)


def router(service):
    route = APIRouter(prefix='/api/yunfollow', tags=['云追'])

    @route.get('/overview')
    async def overview():
        from .offline import jobs
        return {'name': '云追 YunFollow', 'version': '0.3.0', 'tasks': management.tasks(service), 'offline': jobs(service.store),
                'subscriptions': management.subscriptions(service), 'categories': CATEGORIES,
                'search': service.store.state_get('fnos_sources', 'progress', {}),
                'video_transport': '115_only', 'integrations': integrations.latest(service.store)}

    @route.get('/tasks/{task_id}')
    async def job(task_id: str):
        ctx = service.store.get_task(task_id)
        if not ctx or not ctx.raw.extra.get('fnos_subscription'):
            raise ValueError('Workflow task not found')
        return summary(ctx)

    @route.post('/tasks/{task_id}/action')
    async def action(task_id: str, body: Action):
        if body.action == 'classify':
            return await choose(service, task_id, body.destination)
        await management.task_action(service, task_id, body.action)
        return {'recorded': True}

    @route.get('/sources')
    async def sources():
        return Catalog(service.store).public()

    @route.post('/sources')
    async def add_source(body: SourceInput):
        return Catalog(service.store).add(**body.model_dump())

    @route.patch('/sources/{identity}')
    async def update_source(identity: str, body: SourcePatch):
        return Catalog(service.store).update(identity, body.model_dump(exclude_none=True))

    @route.delete('/sources/{identity}')
    async def delete_source(identity: str):
        Catalog(service.store).remove(identity)
        return {'removed': True}

    @route.post('/sources/{identity}/test')
    async def test_source(identity: str):
        return await management.test_source(service, identity)

    @route.post('/sources/{identity}/sync')
    async def sync_source(identity: str):
        if not any(x['id'] == identity for x in Catalog(service.store).items()):
            raise ValueError('Source not found')
        management.wake(service, identity)
        return {'scheduled': True, 'transfer_started': False}

    @route.post('/search')
    async def search():
        management.wake(service)
        return {'scheduled': True}

    @route.get('/candidates')
    async def candidate_batch():
        return candidates.batch(service.store)

    @route.post('/batches/{identity}/next')
    async def next_batch(identity: str):
        return candidates.next_batch(service.store, identity)

    @route.post('/candidates/{identity}/action')
    async def candidate_action(identity: str, body: Action):
        if body.action == 'select':
            return await candidates.select(service, identity)
        if body.action == 'ignore':
            candidates.ignore(service.store, identity)
            return {'ignored': True}
        raise ValueError('Invalid candidate operation')

    @route.get('/integrations')
    async def integrations_status():
        return integrations.latest(service.store)

    @route.post('/integrations/check')
    async def check_integrations():
        return await integrations.check(service)

    @route.post('/subscriptions/refresh')
    async def refresh_subscriptions():
        await management.refresh_airing(service)
        return management.subscriptions(service)

    @route.patch('/subscriptions/{identity}')
    async def subscription_preferences(identity: int, body: PreferencesInput):
        from ..core.subscriptions import key
        from .intake import preferences
        sub = service.store.subscription(identity)
        if not sub:
            raise ValueError('Subscription not found')
        value = service.store.state_get('fnos_subscriptions', key(sub), {})
        value['preferences'] = preferences(body.preferences)
        service.store.state_set('fnos_subscriptions', key(sub), value)
        service.store.state_set('yun_follow', f"{sub['tmdbid']}:{sub['season']}", {'auto_missing': body.auto_missing})
        management.wake(service)
        return {'saved': True}

    @route.get('/rules')
    async def rules():
        naming = service.pipeline.cfg.get('plugins', {}).get('organizer', {}).get('naming', {})
        return {'naming': naming, 'categories': CATEGORIES,
                'priority': ['电影与剧集分开', '剧集先识别动漫', '再识别综艺', '最后按地区归类', '未知分类进入待分类'],
                'note': '新命名只影响后续整理；已有目录迁移需单独检查文件与STRM路径。'}

    @route.patch('/rules')
    async def naming_rules(body: NamingInput):
        from ..plugins.organizer.naming import validate_template
        for kind, template in body.model_dump().items():
            validate_template(template, kind)
            if 'tmdbid' in template.lower():
                raise ValueError('Folder naming must not expose TMDB ID')
        def change(cfg):
            cfg['plugins']['organizer']['naming'] = body.model_dump()
        await service.update_config(change)
        return {'saved': True}

    @route.post('/drafts')
    async def draft(body: IntakeText):
        return await Intake(service).preview(body.text, 'console', body.preferences)

    @route.post('/drafts/{identity}/confirm')
    async def confirm(identity: str, body: Selection):
        result = await Intake(service).confirm(identity, 'console', body.tmdbid, body.season)
        management.wake(service)
        return result

    @route.post('/offline')
    async def offline_draft(body: OfflineInput):
        from .offline import create
        return create(service, body.uri, body.subscription_id)

    @route.post('/offline/{identity}/action')
    async def offline_action(identity: str, body: Action):
        from .offline import confirm, ingest
        if body.action == 'confirm':
            return confirm(service, identity)
        if body.action == 'ingest':
            return await ingest(service, identity)
        raise ValueError('Unsupported offline action')

    return route
