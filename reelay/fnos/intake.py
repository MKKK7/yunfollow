"""Owner-scoped, durable, explicit confirmation before creating a subscription."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
import re

DEFAULT_PREFERENCES = {"min_resolution": "1080p", "subtitles": "zh", "replace_existing": False}


def preferences_from_text(text):
    value = {}
    match = re.search(r'(?i)(?<!\d)(720p|1080p|2160p|4k)(?!\w)', text)
    if match:
        value['min_resolution'] = '2160p' if match[1].lower() == '4k' else match[1].lower()
    if any(word in text for word in ('字幕不限', '不限字幕', '不要求字幕', '无需字幕')):
        value['subtitles'] = 'any'
    elif any(word in text for word in ('中文字幕', '简中', '繁中', '中字')):
        value['subtitles'] = 'zh'
    return value


def preferences(value: dict | None) -> dict:
    result = {**DEFAULT_PREFERENCES, **(value or {})}
    if set(result) != set(DEFAULT_PREFERENCES):
        raise ValueError("Unknown preference")
    if result["min_resolution"] not in ("720p", "1080p", "2160p") or result["subtitles"] not in ("zh", "any") or result["replace_existing"] is not False:
        raise ValueError("Unsupported preference; overwriting existing files is disabled")
    return result


class Intake:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.lock = asyncio.Lock()

    async def preview(self, text: str, owner: str, prefs: dict | None = None) -> dict:
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError("Please provide a title/request of 1..1000 characters")
        self.service.pipeline.reload_config()
        found = await self.service.resolve_subscription(text)
        candidates = found.get("candidates") or []
        if not candidates:
            return {"state": "needs_information", "message": "未找到可靠的 TMDB 剧集，请补充片名和年份，或先配置 TMDB。", "query": found.get("query"), "candidates": []}
        draft = {"id": secrets.token_hex(12), "owner": owner, "created_at": time.time(), "expires_at": time.time() + 900, "state": "awaiting_confirmation", "query": found.get("query"), "candidates": candidates, "preferences": preferences({**preferences_from_text(text), **(prefs or {})})}
        self.store.state_set("fnos_drafts", draft["id"], draft)
        return {k: v for k, v in draft.items() if k != "owner"}

    async def confirm(self, draft_id: str, owner: str, tmdbid: int, season: int) -> dict:
        if type(tmdbid) is not int or type(season) is not int:
            raise ValueError('TMDB ID and season must be integers')
        async with self.lock:
            draft = self.store.state_get("fnos_drafts", draft_id)
            if not draft or draft["owner"] != owner:
                raise ValueError("Draft not found for this owner")
            if draft["state"] == "confirmed":
                if draft["selection"] != [tmdbid, season]:
                    raise ValueError("Draft already confirmed with a different selection")
                return draft["result"]
            if draft["expires_at"] < time.time():
                raise ValueError("Confirmation expired; create a new preview")
            candidate = next((c for c in draft["candidates"] if int(c["tmdbid"]) == tmdbid), None)
            if candidate is None or type(season) is not int or season < 1:
                raise ValueError("Select a returned TMDB candidate and season")
            available = {int(s["season"]) for s in candidate.get("seasons", [])}
            if season not in available:
                raise ValueError("Season is not present in the verified TMDB candidate")
            key = f"tmdb:{tmdbid}:S{season}"
            # Persist desired intent before the idempotent subscription operation.
            intent = {"title": candidate["title"], "tmdbid": tmdbid, "season": season, "auto": True, "preferences": draft["preferences"], "confirmed_at": time.time()}
            self.store.state_set("fnos_subscriptions", key, intent)
            existing = next((s for s in self.store.subscriptions() if s.get("tmdbid") == tmdbid and s["season"] == season), None)
            result = {"subscription": existing} if existing else await self.service.subscribe_show(tmdbid, season)
            draft.update(state="confirmed", selection=[tmdbid, season], result={"state": "subscribed", "subscription": result["subscription"], "preferences": draft["preferences"], "workflow": "wait_for_matching_resource"})
            self.store.state_set("fnos_drafts", draft_id, draft)
            return draft["result"]

    def digest(self, draft_id: str) -> str:
        draft = self.store.state_get("fnos_drafts", draft_id)
        return hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()
