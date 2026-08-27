"""Profile pictures, stories and highlights over the logged-in WEB session.

Why this module exists, and why the buttons kept failing without it.

Instagram has two APIs and this bot talks to both. The MOBILE api is what
instagrapi and instaloader use; the WEB api is what a browser uses. They do
not accept the same credential. A sessionid copied out of a browser is
issued to the web api and is refused by the mobile one - not once, not until
it warms up, but permanently, which is a thing this bot had already learned
the hard way for Direct messages.

Until recently the split ran the wrong way round. Direct messages went over
the web session, where the browser cookie belongs. Stories and profile
pictures went over instagrapi and instaloader - the mobile side - so the one
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

Everything here goes through ig_web.get, which means it inherits the parts
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


# --------------------------------------------------------------------------
# Resolving a username to a user record
#
# Everything else here is addressed by user id, never by name, so this is the
# step that has to work before anything else can.
#
# web_profile_info was the obvious way to do it and it is now returning
#
#     HTTP 400 {"message":"Asset asset://laser.provider/
#               ig_business_category_subvertical has been deleted.
#               You cannot use this schema"}
#
# which is Instagram failing to serialise its own response: a field was
# removed from the schema that endpoint still declares. Nothing about the
# request is wrong, and no header or cookie fixes it - other Instagram tools
# hit exactly the same wall at the same time. It is simply retired.
#
# So there is more than one route, and the first one that answers wins. A
# retired route is remembered so its 400 is paid for once per restart rather
# than once per button press.
# --------------------------------------------------------------------------

_RETIRED_SCHEMA = ("has been deleted", "cannot use this schema")
_retired: set[str] = set()


def _is_retired(error: Exception) -> bool:
    text = str(error).lower()
    return all(marker in text for marker in _RETIRED_SCHEMA)


def _via_web_profile_info(username: str) -> dict:
    data = ig_web.get(f"{_BASE}/users/web_profile_info/",
                      {"username": username}, referer=_referer(username))
    user = ((data or {}).get("data") or {}).get("user") or {}
    if not user.get("id"):
        return {}
    return {
        "id": str(user["id"]),
        "username": user.get("username") or username,
        "full_name": user.get("full_name") or "",
        "is_private": bool(user.get("is_private")),
        "pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url") or "",
    }


def _via_topsearch(username: str) -> dict:
    """The search box's own endpoint.

    It answers with the same user records the profile page is built from, and
    it survives because the web app still calls it on every keystroke in the
    search field - an endpoint the product depends on is a much safer thing
    to rely on than one that only tools use.

    The match is exact and case-insensitive: a search for a name returns
    everything like it, and picking the first hit would quietly download a
    different person's stories.
    """
    data = ig_web.get("https://www.instagram.com/web/search/topsearch/",
                      {"context": "blended", "query": username,
                       "include_reel": "true"},
                      referer=_referer())
    for entry in (data or {}).get("users") or []:
        user = entry.get("user") or {}
        if (user.get("username") or "").lower() != username.lower():
            continue
        pk = user.get("pk") or user.get("pk_id") or user.get("id")
        if not pk:
            continue
        return {
            "id": str(pk),
            "username": user.get("username") or username,
            "full_name": user.get("full_name") or "",
            "is_private": bool(user.get("is_private")),
            "pic": user.get("profile_pic_url") or "",
        }
    return {}


_ROUTES = (("web_profile_info", _via_web_profile_info),
           ("topsearch", _via_topsearch))


def profile(username: str) -> dict:
    """The profile record: id, display name, avatar, privacy.

    Raises with what each route actually said. "Could not find that account"
    is the wrong thing to report when the account is fine and an endpoint
    was retired, and telling those apart from the chat is impossible unless
    the message carries the difference.
    """
    username = username.strip().lstrip("@")
    problems: list[str] = []
    for name, route in _ROUTES:
        if name in _retired:
            continue
        try:
            user = route(username)
        except Exception as e:
            if _is_retired(e):
                log.warning("instagram: the %s endpoint is retired (%s) - "
                            "not asking it again this run", name, str(e)[:120])
                _retired.add(name)
            else:
                log.info("instagram: %s failed for %s: %s", name, username, e)
            problems.append(f"{name}: {str(e)[:90]}")
            continue
        if user:
            return user
        problems.append(f"{name}: no match")

    raise LookupError(
        f"«{username}» رو نتونستم پیدا کنم.\n" + "\n".join(problems[:3])
    )


def _hd_pic(user: dict) -> str:
    """Upgrade a thumbnail to the full-size avatar when it is worth a request.

    topsearch returns the small profile_pic_url. users/<id>/info/ carries the
    hd one, and it is the endpoint the DM path already proves works, so this
    is a cheap improvement rather than a new dependency. Falling back to what
    we have keeps a small picture better than an error.
    """
    small = user.get("pic") or ""
    try:
        data = ig_web.get(f"{_BASE}/users/{user['id']}/info/", {},
                          referer=_referer(user.get("username", "")))
        info = (data or {}).get("user") or {}
        hd = ((info.get("hd_profile_pic_url_info") or {}).get("url")
              or info.get("profile_pic_url") or "")
        return str(hd or small)
    except Exception as e:
        log.info("instagram: hd avatar unavailable (%s) - using the thumbnail", e)
        return small


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
    return [u for u in (_media_url(i) for i in (trays[0].get("items") or [])) if u]


def story_urls(username: str) -> tuple[list[str], dict]:
    """Live stories for a username. Returns (urls, profile)."""
    user = profile(username)
    return _reel_urls(user["id"], username), user


def highlights(username: str) -> tuple[list[dict], dict]:
    """The highlight covers on a profile, newest first. Returns (trays, profile).

    Each entry is {"id": "highlight:1234", "title": ..., "count": n}. The id
    keeps its "highlight:" prefix because that is exactly what reels_media
    expects to be handed back.
    """
    user = profile(username)
    data = ig_web.get(f"{_BASE}/highlights/{user['id']}/highlights_tray/", {},
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
    url = _hd_pic(user)
    if not url:
        raise LookupError(f"عکس پروفایل «{username}» رو نداد.")
    return str(url)
