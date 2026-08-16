"""
Turning one raw Instagram DM item into a DirectMessage.

Shared, because Instagram serves the same inbox payload from two different
APIs and only the transport differs:

    i.instagram.com/api/v1/direct_v2/inbox/     the mobile app API
    www.instagram.com/api/v1/direct_v2/inbox/   the web app API

Same json, same `inbox.threads[].items[]`, same item shapes. So the reader
that has a working session uses its own transport and hands the items here.

Extraction is by SHAPE, not by key name. Every name in _MEDIA_KEYS is one
Instagram has already renamed at least once - reel_share became clip, became
xma_share - and each rename broke a kind of share silently until somebody
reported it. Shape does not get renamed.
"""

from __future__ import annotations

import re

# Keys that hold a shared media object, most specific first.
_MEDIA_KEYS = (
    "clip", "media_share", "story_share", "reel_share", "media",
    "visual_media", "raven_media", "direct_media", "felix_share",
)

# Cross-app shares: a bundle of urls rather than a media object.
_XMA_KEYS = (
    "xma_media_share", "xma_share", "xma_reel_share", "xma_story_share",
    "generic_xma", "xma_link_share",
)

_URL_FIELDS = ("target_url", "url", "video_url", "preview_url", "playable_url")

# Anything under these describes the sender, not the share - and a user object
# carries a pk and image_versions2 too, which would be downloaded as if it
# were the shared media.
_SKIP_KEYS = {"user", "users", "sender", "inviter", "from_user", "reactions",
              "preview_medias", "profile_pic_url"}

SHORTCODE_IN_URL = re.compile(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]{5,})", re.I)

# A shared story arrives as an xma_story_share whose target_url is
#   /stories/<username>/<story_pk>?reel_id=...
# The pk in that path IS the address - a story has no shortcode and is not
# reachable at /p/<code> at all.
STORY_IN_URL = re.compile(r"/stories/[^/?#]+/(\d+)")


def to_epoch(raw) -> float:
    """A DM item's timestamp as seconds, whatever unit it arrived in.

    This was hardcoded to microseconds, which is what the mobile api sends.
    Getting it wrong is not a small error and it is not visible: the poll loop
    only dispatches messages newer than a high-water mark, so a timestamp
    divided by a million too many lands in 1970, every message tests as older
    than the mark, and the inbox reads perfectly while nothing is ever
    delivered. /srcstatus said it exactly:

        ✅ web (فعال): web api reachable
           ↳ 0 پیام · آخری هیچ‌وقت

    So the unit is derived from the magnitude instead of assumed. Present-day
    epochs are ~1.7e9 seconds, ~1.7e12 milliseconds, ~1.7e15 microseconds -
    three orders of magnitude apart, so there is no ambiguity to get wrong.
    """
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0

    if value > 1e14:        # microseconds
        return value / 1_000_000
    if value > 1e11:        # milliseconds
        return value / 1_000
    return value            # already seconds


def best_url(node: dict) -> str:
    for version in node.get("video_versions") or []:
        if version.get("url"):
            return str(version["url"])
    candidates = ((node.get("image_versions2") or {}).get("candidates")) or []
    if candidates and candidates[0].get("url"):
        return str(candidates[0]["url"])
    return ""


def from_media_node(node) -> tuple[str, str, str]:
    """(permalink, pk, url) from something that looks like a media object."""
    if isinstance(node, (list, tuple)):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return "", "", ""

    # story_share and reel_share wrap the real media one level down.
    inner = node.get("media")
    if isinstance(inner, dict):
        found = from_media_node(inner)
        if any(found):
            return found

    code = str(node.get("code") or "")
    pk = str(node.get("pk") or node.get("id") or "")
    url = best_url(node)

    if code:
        return f"https://www.instagram.com/p/{code}/", pk, ""
    if pk:
        return "", pk, url
    if url:
        return "", "", url
    return "", "", ""


def walk_json(node, depth: int = 0) -> tuple[str, str, str]:
    """Last resort: find the media anywhere in the item, by shape."""
    if depth > 5:
        return "", "", ""

    if isinstance(node, (list, tuple)):
        for item in node:
            found = walk_json(item, depth + 1)
            if any(found):
                return found
        return "", "", ""

    if not isinstance(node, dict):
        return "", "", ""

    looks_like_media = "code" in node or (
        ("pk" in node or "id" in node)
        and ("video_versions" in node or "image_versions2" in node)
    )
    if looks_like_media:
        found = from_media_node(node)
        if any(found):
            return found

    for key, value in node.items():
        if key in _SKIP_KEYS:
            continue
        found = walk_json(value, depth + 1)
        if any(found):
            return found
    return "", "", ""


def media_from_item(item: dict) -> tuple[str, str, str]:
    """(permalink, media_pk, media_url) for whatever this DM item shared."""
    for key in _MEDIA_KEYS:
        found = from_media_node(item.get(key))
        if any(found):
            return found

    for key in _XMA_KEYS:
        node = item.get(key)
        if isinstance(node, (list, tuple)):
            node = node[0] if node else None
        if not isinstance(node, dict):
            continue
        urls = [str(node.get(field) or "") for field in _URL_FIELDS]
        # An address is worth far more than a signed url here: fetching an
        # xma video_url returned 600KB of login-wall HTML.
        for candidate in urls:
            if "instagram.com/" not in candidate:
                continue
            story = STORY_IN_URL.search(candidate)
            if story:
                return "", story.group(1), ""
            if SHORTCODE_IN_URL.search(candidate):
                return candidate, "", ""

        for field in ("original_media_igid", "media_id", "target_media_id"):
            value = str(node.get(field) or "")
            if value.split("_")[0].isdigit():
                return "", value, ""

        raw = next((u for u in urls if u), "")
        if raw:
            return "", "", raw

    # The item itself sometimes names the media outright. Seen on a real
    # xma_story_share, whose key list included original_media_igid.
    for field in ("original_media_igid", "media_id", "original_media_id"):
        value = str(item.get(field) or "")
        if value.split("_")[0].isdigit():
            return "", value, ""

    return walk_json(item)


def to_direct_message(item: dict, source: str, me: str = ""):
    """One inbox item -> DirectMessage, or None if it is not ours to handle."""
    from modules.ig_direct import DirectMessage

    sender = str(item.get("user_id") or "")
    if not sender or (me and sender == me):
        return None

    permalink, media_id, media_url = media_from_item(item)
    text = str(item.get("text") or "")
    if not (permalink or media_id or media_url or text):
        return None

    return DirectMessage(
        igsid=sender,
        mid=str(item.get("item_id") or ""),
        text=text,
        media_url=media_url,
        permalink=permalink,
        media_id=media_id,
        timestamp=to_epoch(item.get("timestamp")),
        source=source,
        # The item's own keys. When extraction finds nothing this is the only
        # thing that says where the media was hiding.
        raw={
            "item_type": str(item.get("item_type") or ""),
            "keys": sorted(k for k in item if item.get(k) is not None),
        },
    )
