"""
One proxy url, spelled the way each http library expects.

This exists because the same bug was fixed three separate times and came back
a fourth. `socks5h://` means "resolve DNS at the proxy" to curl and requests.
aiohttp-socks, httpx and aiograpi all reject the "h" suffix outright, before a
single request is made:

    ValueError: Unknown scheme for proxy URL URL('socks5h://127.0.0.1:40000')

They resolve at the proxy anyway, so the suffix is dropped rather than
translated. Every library that takes a proxy in this project goes through
here, so the next one added inherits the fix instead of rediscovering it.
"""

from __future__ import annotations

# (what people write, what the strict libraries accept)
_EQUIVALENT = (("socks5h://", "socks5://"), ("socks4a://", "socks4://"))


def normalize(proxy: str | None) -> str | None:
    """The url with any DNS-at-the-proxy suffix removed. None stays None."""
    if not proxy:
        return None
    lowered = proxy.lower()
    for spelling, plain in _EQUIVALENT:
        if lowered.startswith(spelling):
            return plain + proxy[len(spelling):]
    return proxy


def is_socks(proxy: str | None) -> bool:
    return bool(proxy and proxy.lower().startswith("socks"))
