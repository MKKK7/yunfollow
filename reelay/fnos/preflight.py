"""Read-only checks before creating a visible task. No receive/move/upload APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import asdict

from ..core import confidence, runtime
from ..core.ai import scrub
from ..core.auth115 import is_auth_failure
from ..core.contracts import Decision, ParsedResource, PipelineContext
from ..core.registry import register
from ..providers.parser import LLMParser


def raw_digest(raw):
    return hashlib.sha256(json.dumps({'text': raw.text, 'urls': raw.urls, 'target': raw.extra.get('fnos_subscription')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity_issue(parsed, meta, target):
    identity = meta.get('tmdb', {})
    if parsed.media_type == 'movie' or identity.get('kind') == 'movie':
        return 'movie'
    if not identity.get('matched'):
        return 'identity_unverified'
    if str(identity.get('tmdbid')) != str(target['tmdbid']):
        return 'different_work'
    if meta.get('parse', {}).get('fallback'):
        return 'model_unavailable'
    return None


class Preflight:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.store = pipeline.store

    def audit(self, identity, source_id, title, verdict, reason):
        items = self.store.state_get('fnos_preflight', 'audit', {})
        items[identity] = {'id': identity, 'source_id': source_id, 'title': scrub(title)[:160], 'verdict': verdict,
                           'reason': reason, 'checked_at': time.time()}
        items = dict(sorted(items.items(), key=lambda pair: pair[1]['checked_at'], reverse=True)[:200])
        self.store.state_set('fnos_preflight', 'audit', items)

    async def check(self, raw, candidate, source_id, target):
        self.pipeline.reload_config()
        digest = raw_digest(raw)
        identity = hashlib.sha256((source_id + digest).encode()).hexdigest()[:24]
        cache = self.store.state_get('fnos_preflight', identity, {})
        if cache.get('verdict') == 'excluded' and time.time() - cache.get('at', 0) < 1800:
            return cache['verdict'], cache['reason']
        storage = self.pipeline.reg.get_provider('storage', 'p115')
        share = candidate['share']
        probe = ParsedResource(is_media=True, title=target['title'], media_type='tv', season=target['season'],
                               share_code=share['shareCode'], receive_code=share.get('password'))
        try:
            # Check expiration before paying for model inference or constructing a task.
            with self.pipeline.scope():
                facts = await asyncio.to_thread(storage.inspect_share, probe, describe=True)
            status = facts.get('status')
            if facts.get('evidence_complete') is False:
                status = 'incomplete'
            if status == 'unavailable':
                verdict, reason = ('deferred', '115_auth_required') if is_auth_failure(facts.get('note')) else ('excluded', 'share_unavailable')
            elif status != 'ok':
                verdict, reason = 'deferred', 'episode_files_unverified'
                if status == 'issue' and facts.get('file_evidence'):
                    from ..core.media import episode_of
                    if all(episode_of(x['name'])[1] is None for x in facts['file_evidence']):
                        verdict, reason = 'excluded', 'not_episodic_files'
                # Explicit movie metadata plus no episodic files is a conclusive exclusion.
                if candidate.get('media_type') == 'movie':
                    verdict, reason = 'excluded', 'movie'
            else:
                evidence = {'files': sorted(facts.get('file_evidence', []), key=lambda f: (f['name'], f.get('size') or 0)),
                            'pairs': sorted(facts.get('pairs', []))}
                file_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
                raw.extra['fnos_file_evidence'] = {'pairs': facts.get('pairs', []),
                    'files': facts.get('file_evidence', [])[:200], 'resolution': facts.get('resolution')}
                if cache.get('verdict') == 'accepted' and cache.get('file_hash') == file_hash:
                    # The link and actual files were read again. Reuse only model work.
                    cache.update(at=time.time())
                    key = identity + ':' + file_hash[:16]
                    self.store.state_set('fnos_preflight', identity, cache)
                    self.store.state_set('fnos_preflight', key, cache)
                    raw.extra['fnos_preflight'] = key
                    self.audit(identity, source_id, candidate['title'], 'accepted', cache['reason'])
                    return 'accepted', cache['reason']
                ctx = PipelineContext(raw=raw)
                ctx.source_rule = self.pipeline.rule_for(raw.source)
                self.pipeline.stamp_config(ctx)
                with self.pipeline.scope(ctx):
                    ctx.parsed = await self.pipeline.reg.get_provider('parser', 'llm').parse(ctx)
                await self.pipeline.run_hooks('after_parse', ctx)
                confidence.apply(ctx)
                problem = identity_issue(ctx.parsed, ctx.meta, target)
                if problem:
                    verdict = 'excluded' if problem in ('movie', 'different_work') else 'deferred'
                    reason = problem
                elif ctx.meta.get('share', {}).get('status') == 'unavailable':
                    verdict, reason = 'excluded', 'share_unavailable'
                else:
                    with self.pipeline.scope(ctx):
                        decision = await self.pipeline.reg.get_provider('decision', 'fnos_subscription').decide(ctx)
                    if decision == Decision.SKIP:
                        verdict, reason = 'excluded', 'subscription_rules'
                    else:
                        verdict, reason = 'accepted', ('ready' if decision == Decision.SAVE else 'quality_review')
                        cache = {'parsed': asdict(ctx.parsed), 'meta': {k: ctx.meta[k] for k in ('parse', 'tmdb', 'share') if k in ctx.meta},
                                 'digest': digest, 'source': raw.source, 'target': target, 'file_hash': file_hash}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            verdict, reason = 'deferred', 'probe_error:' + type(exc).__name__
        # Only bounded, sanitized diagnostics are exposed. Share passwords/URLs stay private.
        value = {**cache, 'verdict': verdict, 'reason': reason, 'at': time.time()}
        self.store.state_set('fnos_preflight', identity, value)
        if verdict == 'excluded' and reason == 'share_unavailable':
            for key, item in self.store.state_items('yun_candidates').items():
                if item.get('candidate', {}).get('share', {}).get('shareCode') == share.get('shareCode'):
                    item['state'] = 'unavailable'
                    self.store.state_set('yun_candidates', key, item)
        self.audit(identity, source_id, candidate['title'], verdict, reason)
        key = identity + ':' + value['file_hash'][:16] if verdict == 'accepted' else identity
        if verdict == 'accepted':
            self.store.state_set('fnos_preflight', key, value)
        raw.extra['fnos_preflight'] = key
        return verdict, reason


@register(kind='provider', ptype='parser')
class VerifiedParser(LLMParser):
    name = 'fnos_verified'

    async def parse(self, ctx):
        key = ctx.raw.extra.get('fnos_preflight')
        value = runtime.services().store.state_get('fnos_preflight', key, {}) if isinstance(key, str) else {}
        if (value.get('verdict') == 'accepted' and value.get('source') == ctx.raw.source
                and value.get('digest') == raw_digest(ctx.raw) and time.time() - value.get('at', 0) < 1200):
            ctx.meta.update(value['meta'])
            ctx.ns('fnos_workflow')['preflight'] = {'state': 'done', 'checked_at': value['at'], 'reason': value['reason']}
            ctx.log('已通过入队前有效性、季集文件和剧集身份核验；复用受校验的识别结果')
            return ParsedResource(**value['parsed'])
        return await super().parse(ctx)
