"""
Shared pooled HTTP client.

Building an httpx client per call pays a fresh DNS lookup plus a full TLS
handshake every time - several hundred milliseconds on every metadata lookup,
cover download and lyrics fetch. One long-lived client keeps connections warm,
so the second request to a host costs only the round trip.
"""

from __future__ import annotations

import threading

import httpx

_lock = threading.Lock()
_client: httpx.Client | None = None

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def client() -> httpx.Client:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=httpx.Timeout(8.0, connect=4.0),
                    follow_redirects=True,
                    limits=httpx.Limits(
                        max_keepalive_connections=20,
                        max_connections=40,
                        keepalive_expiry=120.0,
                    ),
                    headers={"User-Agent": _UA},
                )
    return _client


def get(url: str, **kwargs) -> httpx.Response:
    return client().get(url, **kwargs)


# Cover art gets fetched up to three times per track: to embed in the file, to
# build the 320x320 Telegram thumbnail, and sometimes to send as a photo. Keep
# the bytes around instead.
_MAX_CACHED = 64
_bytes_cache: "OrderedDict[str, tuple[bytes, str]]" = None  # type: ignore[assignment]


def get_bytes(url: str) -> tuple[bytes, str] | None:
    """Fetch a binary resource, memoised. Returns (data, content_type)."""
    global _bytes_cache
    if _bytes_cache is None:
        from collections import OrderedDict

        _bytes_cache = OrderedDict()

    if not url:
        return None
    with _lock:
        hit = _bytes_cache.get(url)
        if hit is not None:
            _bytes_cache.move_to_end(url)
            return hit
    try:
        r = get(url)
        r.raise_for_status()
        value = (r.content, r.headers.get("content-type", "image/jpeg").split(";")[0])
    except Exception:
        return None
    with _lock:
        _bytes_cache[url] = value
        while len(_bytes_cache) > _MAX_CACHED:
            _bytes_cache.popitem(last=False)
    return value
