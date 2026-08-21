"""
Audius as a third audio source, for the 320kbps neither of the other two has.

Why this one, out of everything yt-dlp supports.

The ceiling on what this bot could deliver was set by its two sources:
YouTube tops out around 130-146kbps and SoundCloud's streams at 160. Asking
for 320 was never going to work, because 320 was not there to be had.

Audius has it. Uploaders publish the original file and it is served whole -
measured on a real track, 9.0MB for 3:55, which is 320kbps exactly. yt-dlp
already has an extractor, so nothing here downloads anything: this module
only turns a search into a permalink and hands it over.

It is also the only music source found that needs no key, no cookie and no
account. Checked against the alternatives before settling on it:

    RadioJavan   the extractor covers /videos/video/ only - music videos,
                 not audio - and the site answers this server with 403.
    Audiomack    api.audiomack.com wants a key (401); the site's own search
                 endpoint answers, but there is no extractor to follow it up.
    Bandcamp     search works and the extractor is good, but the catalogue is
                 independent western releases - close to nothing for the
                 audience this bot actually has.

What Audius is not: a complete catalogue. It is artist-uploaded, so it has
Persian rap and remixes in depth and mainstream western chart music barely at
all. That is why it is a third source rather than a replacement - it is asked
alongside the others and wins on merit through the same scoring as the rest.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The public discovery node. No key, no account, no rate limit published.
# app_name is required by their API and is only used for their own analytics.
_SEARCH = "https://discoveryprovider.audius.co/v1/tracks/search"
_APP = "moeinimydl-bot"

# Their search is fuzzy and unranked beyond relevance, so a wide net here
# would mostly add noise for _match_entry to reject. Six is the same depth
# the other sources are searched to.
_LIMIT = 6


def search(query: str, limit: int = _LIMIT) -> list[dict]:
    """Flat entries shaped like yt-dlp's, so the existing scoring can read them.

    Returns [] for anything that goes wrong. A third source that raises is
    worse than a third source that is quiet: the other two still have answers.
    """
    from utils import http

    try:
        r = http.get(_SEARCH, params={"query": query, "limit": limit,
                                      "app_name": _APP}, timeout=8)
        if r.status_code != 200:
            log.info("audius search: HTTP %s", r.status_code)
            return []
        tracks = (r.json() or {}).get("data") or []
    except Exception as e:
        log.info("audius search failed for %r: %s", query, e)
        return []

    out = []
    for t in tracks:
        permalink = t.get("permalink") or ""
        if not permalink:
            continue
        user = (t.get("user") or {}).get("name") or ""
        out.append({
            # The keys _match_entry reads. Shaped like a yt-dlp flat entry so
            # nothing downstream needs to know Audius exists.
            "id": t.get("id"),
            "title": t.get("title") or "",
            "url": f"https://audius.co{permalink}",
            "duration": t.get("duration") or 0,
            "uploader": user,
            "channel": user,
        })
    return out
