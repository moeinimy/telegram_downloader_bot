"""Profile pictures, stories and highlights over the logged-in WEB session.

Why this module exists, and why the buttons kept failing without it.

Instagram has two APIs and this bot talks to both. The MOBILE api is what
instagrapi and instaloader use; the WEB api is what a browser uses. They do
not accept the same credential. A sessionid copied out of a browser is
issued to the web api and is refused by the mobile one - not once, not until
it warms up, but permanently, which is a thing this bot has already learned
the hard way for Direct messages.

Until now the split ran the wrong way round. Direct messages went over the
web session, where the browser cookie belongs. Stories and profile pictures
went over instagrapi and instaloader - the mobile side - so the one
credential the operator can actually obtain was wired to the one feature
that could not use it. Both "options" the error message offered led there,
which is why both of them appeared broken.

The anonymous route that used to cover profile pictures is gone as well, and
not because of a bug here: unavatar.io moved its Instagram provider behind a
paid plan and now answers

    403  {"code":"EPRO","message":"This provider requires a pro plan"}

The scraper mirrors that were the obvious replacements (picuki, imginn) both
answer 403 to this server too, and Instagram's own web_profile_info answers
429 without a session. There is no longer a logged-out way to fetch a
profile picture, so this path is the only one, rather than a fallback.

Everything here goes through ig_web._get, which means it inherits the parts
that took a while to get right: the rotated X-IG-WWW-Claim, the rotated
sessionid being written back, checkpoint detection, and the request counter
that the rate display reads. Nothing here opens its own connection.
"""

from __future__ import annotations

import logging

from modules import ig_web

log = logging.getLogger(__name__)

_BASE = "https://www.instagram.com/api/v1"


def usable() -> bool:
    return ig_web.usable()


def _referer(username: str = "") -> str:
    return f"https://www.instagram.com/{username}/" if username else \
        "https://www.instagram.com/"


def profile(username: str) -> dict:
    """The public profile record: id, display name, avatar, privacy.

    The user id is the thing everything else here needs - stories and
    highlights are both addressed by id, never by username.
    """
    data = ig_web.get(
        f"{_BASE}/users/web_profile_info/", {"username": username},
        referer=_referer(username),
    )
    user = ((data or {}).get("data") or {}).get("user")
    if not user:
        raise LookupError(f"اکانت «{username}» پیدا نشد.")
    return user


def _media_url(item: dict) -> str:
    """The best rendition of one story item.

    Video first: a story that has both is a video, and the image is only its
    cover frame. Sending the cover instead of the clip looks like the
    download worked and quietly loses what was asked for.
    """
    videos = item.get("video_versions") or []
    if videos:
        return str(videos[0].get("url") or "")
    candidates = ((item.get("image_versions2") or {}).get("candidates") or [])
    if candidates:
        return str(candidates[0].get("url") or "")
    return ""


def _reel_urls(reel_id: str, username: str = "") -> list[str]:
    """Media urls for one reel tray - a user's live stories, or a highlight.

    Both are the same endpoint and the same item shape; only the id differs,
    which is why highlights cost no new plumbing once stories work.
    """
    data = ig_web.get(f"{_BASE}/feed/reels_media/", {"reel_ids": reel_id},
                      referer=_referer(username))
    trays = (data or {}).get("reels_media") or []
    if not trays:
        # reels_media answers 200 with an empty list both for "no stories
        # right now" and for "you cannot see this account's stories". They
        # are different situations and the caller says so; here they are the
        # same empty answer.
        return []
    urls = [u for u in (_media_url(i) for i in (trays[0].get("items") or [])) if u]
    return urls


def story_urls(username: str) -> tuple[list[str], dict]:
    """Live stories for a username. Returns (urls, profile)."""
    user = profile(username)
    return _reel_urls(str(user.get("id") or ""), username), user


def highlights(username: str) -> tuple[list[dict], dict]:
    """The highlight covers on a profile, newest first. Returns (trays, profile).

    Each entry is {"id": "highlight:1234", "title": ..., "count": n}. The id
    keeps its "highlight:" prefix because that is exactly what reels_media
    expects to be handed back.
    """
    user = profile(username)
    uid = str(user.get("id") or "")
    data = ig_web.get(f"{_BASE}/highlights/{uid}/highlights_tray/", {},
                      referer=_referer(username))
    out: list[dict] = []
    for tray in (data or {}).get("tray") or []:
        tray_id = str(tray.get("id") or "")
        if not tray_id:
            continue
        out.append({
            "id": tray_id,
            "title": str(tray.get("title") or "بدون اسم"),
            "count": int(tray.get("media_count") or 0),
        })
    return out, user


def highlight_urls(highlight_id: str, username: str = "") -> list[str]:
    """Media urls inside one highlight."""
    if not highlight_id.startswith("highlight:"):
        highlight_id = f"highlight:{highlight_id}"
    return _reel_urls(highlight_id, username)


def profile_pic_url(username: str) -> str:
    user = profile(username)
    url = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    if not url:
        raise LookupError(f"عکس پروفایل «{username}» رو نداد.")
    return str(url)
