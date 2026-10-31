"""Bounded API JSON requests; never logs response bodies, credentials or signed URLs."""
import asyncio
import json
import urllib.error
import urllib.request


class IntegrationError(RuntimeError):
    pass


def request(url, data=None, *, method=None, headers=None, limit=8 * 1024 * 1024, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise IntegrationError("API response exceeds limit")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise IntegrationError(f"API HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise IntegrationError("API connection failed") from None


async def api(*args, **kwargs):
    return await asyncio.to_thread(request, *args, **kwargs)
