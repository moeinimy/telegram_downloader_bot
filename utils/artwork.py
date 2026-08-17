"""The largest version of a cover the source will serve.

Every cover in this bot arrives already shrunk. iTunes hands back a 100x100
thumbnail that the search code rewrites to 600x600; Deezer's `cover_xl` is
1000x1000; YouTube thumbnails come in whatever size the entry happened to
carry. Those sizes are chosen to be sent as a Telegram photo, where anything
larger is recompressed anyway.

A cover asked for as a *file* has no such ceiling, and the same CDNs will
serve much larger versions of the identical image if the url asks for them.
Both of these are the size written into the path rather than a separate
asset, so upgrading is a rewrite, not another lookup:

    .../source/ab/cd/.../100x100bb.jpg      ->  .../3000x3000bb.jpg
    .../cover/<hash>/1000x1000-000000-80-0-0.jpg -> .../1800x1800-000000-100-0-0.jpg

Nothing here trusts that a bigger one exists: each candidate is offered in
descending order and the caller takes the first that actually responds, so a
source that stops serving a size degrades to the next one down rather than
to nothing.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Apple's CDN builds the size into the filename and will render on demand.
# 3000 is comfortably past what any release actually contains, and it answers
# with the largest it has rather than an error.
_APPLE_SIZES = ("3000x3000bb", "1400x1400bb", "600x600bb")
_APPLE_RE = re.compile(r"/\d+x\d+(bb)?\.(jpg|png|webp)", re.I)

# Deezer's path is <size>-<background>-<quality>-0-0.jpg. Quality is a JPEG
# setting, so it is worth raising along with the pixels.
_DEEZER_SIZES = ("1800x1800-000000-100-0-0", "1000x1000-000000-100-0-0")
_DEEZER_RE = re.compile(r"/\d+x\d+-[\d\w]+-\d+-\d+-\d+\.(jpg|png)", re.I)

# YouTube publishes a fixed ladder of names rather than arbitrary sizes, and
# maxresdefault is missing on plenty of videos - hence trying downwards.
_YT_SIZES = ("maxresdefault", "sddefault", "hqdefault")
_YT_RE = re.compile(r"/(maxres|sd|hq|mq)default\.(jpg|webp)", re.I)


def candidates(url: str) -> list[str]:
    """Every version worth trying, largest first, the original always last.

    The original is kept as the final entry on purpose: it is the one url
    already known to work, so the list can never end up empty or worse than
    what the caller already had.
    """
    if not url:
        return []

    out: list[str] = []

    if _APPLE_RE.search(url):
        ext = url.rsplit(".", 1)[-1]
        out += [_APPLE_RE.sub(f"/{size}.{ext}", url) for size in _APPLE_SIZES]
    elif _DEEZER_RE.search(url):
        ext = url.rsplit(".", 1)[-1]
        out += [_DEEZER_RE.sub(f"/{size}.{ext}", url) for size in _DEEZER_SIZES]
    elif _YT_RE.search(url):
        ext = url.rsplit(".", 1)[-1]
        out += [_YT_RE.sub(f"/{name}.{ext}", url) for name in _YT_SIZES]

    out.append(url)

    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def best(url: str) -> tuple[str, bytes] | None:
    """Fetch the largest version that actually answers.

    Returns (url, bytes), or None when even the original cannot be fetched.
    A candidate is accepted only if it is an image and is not smaller than
    what a plain request for the original would have produced - some CDNs
    answer an oversized request with a placeholder rather than a 404.
    """
    from utils import http

    smallest_useful = 8 * 1024

    for candidate in candidates(url):
        try:
            r = http.client().get(candidate, timeout=20)
        except Exception as e:
            log.info("artwork: %s did not answer (%s)", candidate[-40:], e)
            continue

        if r.status_code != 200:
            continue
        if "image" not in r.headers.get("content-type", ""):
            continue
        if len(r.content) < smallest_useful:
            # A 2KB answer to a 3000px request is a placeholder, not artwork.
            log.info("artwork: %s returned %dB - too small to be the cover",
                     candidate[-40:], len(r.content))
            continue

        return candidate, r.content

    return None
