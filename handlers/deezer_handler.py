"""Deezer links.

Almost nothing here is new. Deezer has been a metadata source for a while -
`_deezer_search` produces `dz_<id>` tracks, `get_track_meta` re-fetches them
by id after a restart, and `download_track` locates the audio the same way it
does for a Spotify link. The only thing missing was the URL: a Deezer link
fell through to the free-text search, which then searched for the URL itself.

So this resolves a link to the id that machinery already understands, and
hands it over.

WHAT THIS DOES NOT DO is download from Deezer. Their streams are encrypted
and the key comes from an ARL - the cookie of a logged-in, paying account -
so "adding Deezer as a source" means shipping DRM circumvention tied to
somebody's subscription. That is a different kind of thing from reading a
public API, and it is not something to slip in as an implementation detail.
What this gets instead is Deezer's catalogue for IDENTIFYING a track, which
is the half that makes the search better: their API returns the ISRC and the
full contributor list, and both make the match more accurate than a title
string can.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from modules import spotify as sp
from utils.i18n import Localised, localise, t
from utils.secrets import scrub
from utils.url_router import RouteResult

log = logging.getLogger(__name__)

_API = "https://api.deezer.com"


def _resolve_short(url: str) -> str:
    """Follow deezer.page.link to the real one, or return it unchanged."""
    from utils import http

    try:
        return str(http.get(url).url)
    except Exception as e:
        log.info("deezer: could not resolve %s (%s)", url, e)
        return url


def _tracks_of(kind: str, resource_id: str) -> list[dict]:
    """The track records behind an album or a playlist."""
    from utils import http

    response = http.get(f"{_API}/{kind}/{resource_id}")
    data = response.json() or {}
    if data.get("error"):
        raise Localised("این لینک دیزر رو نتونستم باز کنم.")
    return list((data.get("tracks") or {}).get("data") or [])


def _to_meta(record: dict) -> sp.TrackMeta:
    """One Deezer record as the bot's own track shape.

    contributors rather than just `artist`: Deezer lists every credited name,
    and a featured artist is exactly what somebody searching for the track
    typed. The bot already resolves credits before locating audio for this
    reason - here they arrive for free.
    """
    artists = [c.get("name") for c in (record.get("contributors") or [])
               if c.get("name")]
    if not artists:
        artists = [((record.get("artist") or {}).get("name") or "")]
    album = record.get("album") or {}
    return sp.TrackMeta(
        id=f"dz_{record.get('id')}",
        name=record.get("title") or record.get("title_short") or "",
        artists=[a for a in artists if a],
        album=album.get("title") or "",
        duration_ms=int(record.get("duration") or 0) * 1000,
        cover_url=(album.get("cover_xl") or album.get("cover_big")
                   or album.get("cover") or ""),
        spotify_url=record.get("link") or "",
        isrc=record.get("isrc") or "",
        credits_done=bool(artists),
    )


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     route: RouteResult) -> None:
    from handlers import spotify_handler

    msg = update.effective_message
    kind, resource_id = route.kind, route.resource_id

    if kind == "short":
        from utils.helpers import run_in_thread
        from utils.url_router import route as reroute

        resolved = await run_in_thread(_resolve_short)(route.url)
        again = reroute(resolved)
        if again is None or again.kind == "short":
            await msg.reply_text(t(msg.chat_id, "این لینک دیزر رو نتونستم باز کنم."))
            return
        kind, resource_id = again.kind, again.resource_id

    if kind == "track":
        # Fetched here rather than left to get_track_meta, because the two
        # do not return the same thing. Measured on this track:
        #
        #   get_track_meta  artists ['Drake']            isrc ''
        #   this            artists ['Drake', 'J. Cole'] isrc 'USUG12306071'
        #
        # The credit is the half that finds the audio: YouTube titles it
        # "Drake - First Person Shooter ft. J. Cole", and a search built from
        # the lead artist alone is missing the word somebody would type.
        #
        # Putting it in the cache is how it reaches the rest: get_track_meta
        # reads that first, so every later button on this track gets the
        # richer record too.
        from utils.helpers import run_in_thread
        from utils import http

        try:
            record = await run_in_thread(
                lambda: http.get(f"{_API}/track/{resource_id}").json())()
            if record and not record.get("error"):
                sp._yt_cache[f"dz_{resource_id}"] = _to_meta(record)
        except Exception as e:
            log.info("deezer: could not enrich %s (%s) - falling back to the "
                     "id lookup", resource_id, e)

        # dz_<id> is a shape get_track_meta already knows how to re-fetch, so
        # from here on this is the same path a Spotify link takes.
        await spotify_handler.handle_url(
            update, context,
            RouteResult(platform=route.platform, kind="track",
                        url=route.url, resource_id=f"dz_{resource_id}"),
        )
        return

    if kind in ("album", "playlist"):
        from utils.helpers import run_in_thread

        status = await msg.reply_text(
            t(msg.chat_id, "🔎 در حال گرفتن اطلاعات آهنگ…"))
        try:
            records = await run_in_thread(_tracks_of)(kind, resource_id)
        except Exception as e:
            await status.edit_text("❌ " + scrub(localise(msg.chat_id, e)))
            return
        tracks = [_to_meta(r) for r in records if r.get("id")]
        if not tracks:
            await status.edit_text(t(msg.chat_id, "این لینک دیزر رو نتونستم باز کنم."))
            return
        # Cached so the buttons still work after a restart, exactly as the
        # search results are.
        for meta in tracks:
            sp._yt_cache[meta.id] = meta
        await status.delete()
        await spotify_handler._send_tracklist(
            msg, title=f"💿 {len(tracks)}", tracks=tracks, bulk_callback=None)
        return

    await msg.reply_text(t(msg.chat_id, "این لینک دیزر رو نتونستم باز کنم."))
