from ..core import runtime, subscriptions
from ..core.contracts import Decision
from ..core.registry import register
from ..providers.decision import SubscriptionDecision


def quality(parsed, prefs):
    ranks = {"720p": 720, "1080p": 1080, "2160p": 2160, "4k": 2160}
    resolution = ranks.get(str(parsed.resolution or "").lower())
    required = ranks[prefs.get("min_resolution", "1080p")]
    if resolution is not None and resolution < required:
        return "reject"
    languages = (parsed.release or {}).get("subtitles") or []
    if isinstance(languages, str):
        languages = [languages]
    chinese = any(str(x).lower() in ("chs", "cht", "zh", "chi", "简体中字", "中文字幕") for x in languages)
    if resolution is None or (prefs.get("subtitles", "zh") == "zh" and not chinese):
        return "review"
    return "accept"


@register(kind="provider", ptype="decision")
class FnosDecision(SubscriptionDecision):
    name = "fnos_subscription"

    async def decide(self, ctx):
        if ctx.meta.get('share', {}).get('status') == 'unavailable':
            ctx.log('FNOS：分享已确认不可用，跳过并继续等待其他资源')
            return Decision.SKIP
        target = ctx.raw.extra.get('fnos_subscription')
        identity = ctx.meta.get('tmdb', {})
        wrong_identity = target and identity.get('matched') and str(identity.get('tmdbid')) != str(target['tmdbid'])
        if target and (ctx.parsed.media_type == 'movie' or identity.get('kind') == 'movie' or wrong_identity):
            ctx.log('FNOS：候选为电影或TMDB身份与已确认剧集不一致，跳过')
            return Decision.SKIP
        original_mode = ctx.source_rule.get('approval_mode')
        ctx.source_rule['approval_mode'] = 'manual'
        try:
            baseline = await super().decide(ctx)
        finally:
            if original_mode is None:
                ctx.source_rule.pop('approval_mode', None)
            else:
                ctx.source_rule['approval_mode'] = original_mode
        if baseline == Decision.SKIP:
            return baseline
        services = runtime.services()
        sub = subscriptions.match(services.subscriptions(), ctx.parsed, ctx.meta.get("tmdb", {}))
        if not sub:
            self.withdraw(ctx)
            return Decision.ASK
        prefs = services.store.state_get("fnos_subscriptions", subscriptions.key(sub), {}).get("preferences", {})
        verdict = quality(ctx.parsed, prefs)
        if verdict == "reject":
            self.withdraw(ctx)
            ctx.log("FNOS：低于已确认的清晰度要求")
            return Decision.SKIP
        if ctx.raw.extra.get('yun_resource_confirmed') and baseline != Decision.SKIP:
            ctx.log('云追：用户已确认此资源版本，按实际文件清单转存')
            return Decision.SAVE
        if verdict == "review" or baseline == Decision.ASK:
            self.withdraw(ctx)
            ctx.log("FNOS：画质、中文字幕或作品匹配未核实，等待人工确认")
            return Decision.ASK
        return baseline
