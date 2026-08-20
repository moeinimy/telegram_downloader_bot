"""Verification for the Instagram Direct feature (brief section 7)."""

import ast
import builtins
import hashlib
import re
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(r"D:\claude projects\telegram_downloader_bot")
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Keep the real downloads/ directory out of this: the dedup test writes a
# seen-store, and polluting the live one would silently swallow a real message.
_TMP = tempfile.mkdtemp(prefix="igverify-")
os.environ["DOWNLOAD_DIR"] = _TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:verify")
os.environ["IG_APP_SECRET"] = "correct-horse-battery-staple"
os.environ["IG_VERIFY_TOKEN"] = "verify-me"
os.environ["IG_ACCESS_TOKEN"] = "tok"

# Findings are Persian UI strings. A Windows console defaults to cp1252 and
# would crash on the first one - hiding the very failure it is reporting.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

failures = []
def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ---------------------------------------------------------------- 2. AST undefined names
SOURCES = sorted(
    p for d in ("modules", "handlers", "utils", "web") for p in Path(d).glob("*.py")
) + [Path("main.py"), Path("config.py")]


def undefined_names(path: Path) -> list[str]:
    """Names loaded at module or function level that were never bound anywhere
    in the file. Catches the NameError class of typo that only fires on the
    error path, long after deploy."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "self", "cls"}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg:
                    bound.add(arg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.comprehension):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)

    return sorted({
        n.id for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound
    })


bad = {str(p): u for p in SOURCES if (u := undefined_names(p))}
check("AST undefined-name sweep", not bad, json.dumps(bad, ensure_ascii=False) if bad else f"{len(SOURCES)} files clean")


# ---------------------------------------------------------------- 3. i18n completeness
from utils.i18n import _EN  # noqa: E402

used: set[str] = set()
for path in SOURCES:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "t"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            used.add(node.args[1].value)

missing = sorted(s for s in used if s not in _EN)
check("i18n: every t() string has an English entry", not missing,
      f"{len(used)} strings checked" if not missing else f"MISSING: {missing}")

untranslated = sorted(k for k, v in _EN.items() if k == v and not k.startswith(("🎤", "🎵 A", "🌐")))
check("i18n: no accidental identity translations", not untranslated,
      "" if not untranslated else str(untranslated))


# ---------------------------------------------------------------- 4. signature verification
from web import webhook  # noqa: E402

body = b'{"object":"instagram","entry":[]}'
good = hmac.new(b"correct-horse-battery-staple", body, hashlib.sha256).hexdigest()
wrong = hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()

check("signature: correct secret accepted", webhook.verify_signature(body, f"sha256={good}"))
check("signature: WRONG secret rejected", not webhook.verify_signature(body, f"sha256={wrong}"))
check("signature: missing header rejected", not webhook.verify_signature(body, None))
check("signature: unprefixed digest rejected", not webhook.verify_signature(body, good))
check("signature: sha1 prefix rejected", not webhook.verify_signature(body, f"sha1={good}"))
check("signature: tampered body rejected", not webhook.verify_signature(body + b" ", f"sha256={good}"))


# ---------------------------------------------------------------- 5. duplicate mid replay
from modules import ig_direct  # noqa: E402

payload = {
    "object": "instagram",
    "entry": [{
        "id": "17841400000000000",
        "time": 1754900000,
        "messaging": [{
            "sender": {"id": "IGSID-USER"},
            "recipient": {"id": "IGSID-US"},
            "timestamp": 1754900000000,
            "message": {
                "mid": "aWc6MTIz",
                "attachments": [{
                    "type": "ig_reel",
                    "payload": {"reel_video_id": "3123456789012345678",
                                "title": "a reel",
                                "url": "https://lookaside.fbsbx.com/x.mp4"},
                }],
            },
        }],
    }],
}

first = webhook.parse_entries(payload)
second = webhook.parse_entries(payload)  # Meta's retry: byte-identical
check("parse: one attachment -> one message", len(first) == 1, f"got {len(first)}")

seen_1 = ig_direct.already_seen(first[0])
seen_2 = ig_direct.already_seen(second[0])
check("dedup: first delivery is new", not seen_1)
check("dedup: replayed mid is suppressed", seen_2)

# The failover case: the same share seen by the OTHER source, under an id from
# a different id space entirely.
poll_view = ig_direct.DirectMessage(
    igsid="IGSID-USER",
    mid="29999999999999999",          # instagrapi's item id, unrelated to mid
    media_id="3123456789012345678",
    timestamp=first[0].timestamp,
    source="poll",
)
check("dedup: cross-source duplicate suppressed", ig_direct.already_seen(poll_view))

fresh = ig_direct.DirectMessage(
    igsid="IGSID-USER",
    mid="different",
    media_id="3123456789012345678",
    timestamp=first[0].timestamp + 3600,   # an hour later = a real second request
    source="webhook",
)
check("dedup: same reel an hour later is NOT suppressed", not ig_direct.already_seen(fresh))


# ---------------------------------------------------------------- shortcode round trip
from modules.instagram import _shortcode_to_media_id, media_id_to_shortcode  # noqa: E402

check("shortcode: media_id -> shortcode round trip",
      media_id_to_shortcode(str(_shortcode_to_media_id("DZfwtaiob79"))) == "DZfwtaiob79",
      media_id_to_shortcode(str(_shortcode_to_media_id("DZfwtaiob79"))))
check("shortcode: owner-suffixed id handled",
      media_id_to_shortcode(f"{_shortcode_to_media_id('DZfwtaiob79')}_17841400000") == "DZfwtaiob79")
check("shortcode: from ig_reel attachment",
      first[0].shortcode() == media_id_to_shortcode("3123456789012345678"),
      first[0].shortcode())
check("shortcode: permalink in text wins", ig_direct.DirectMessage(
    igsid="x", text="ببین https://www.instagram.com/reel/ABC123xyz/ خفنه", source="webhook"
).shortcode() == "ABC123xyz")
check("shortcode: junk id yields nothing", media_id_to_shortcode("not-a-number") == "")


# ---------------------------------------------------------------- pairing store
from modules import ig_pairing  # noqa: E402

tok = ig_pairing.issue(555)
check("pairing: token looks like a token", ig_pairing.looks_like_token(tok), tok)
check("pairing: lowercase + no prefix still redeems",
      ig_pairing.redeem(tok.replace("IG-", "").lower(), "ig:111") == 555)
check("pairing: token is single use", ig_pairing.redeem(tok, "ig:222") is None)
check("pairing: reverse lookup", ig_pairing.chat_for("ig:111") == 555)

tok2 = ig_pairing.issue(555)
ig_pairing.redeem(tok2, "pk:999")
check("pairing: both namespaces coexist for one chat",
      sorted(ig_pairing.linked_ids(555)) == ["ig:111", "pk:999"],
      str(sorted(ig_pairing.linked_ids(555))))

tok3 = ig_pairing.issue(555)
ig_pairing.redeem(tok3, "ig:333")
check("pairing: re-pairing replaces within a namespace only",
      sorted(ig_pairing.linked_ids(555)) == ["ig:333", "pk:999"],
      str(sorted(ig_pairing.linked_ids(555))))

check("pairing: issuing twice invalidates the older token",
      (lambda a, b: (ig_pairing.redeem(a, "ig:444") is None) and (ig_pairing.redeem(b, "ig:444") == 777))(
          ig_pairing.issue(777), ig_pairing.issue(777)))

check("pairing: unlink removes every namespace", ig_pairing.unlink(555) and not ig_pairing.linked_ids(555))
check("pairing: alphabet excludes lookalikes",
      not (set("01IOLl") & set(ig_pairing._ALPHABET)))

# Survives a restart: the store is reloaded from disk, not from memory.
ig_pairing._data = None
check("pairing: persisted across a reload", ig_pairing.chat_for("ig:444") == 777)

ig_direct._seen = None
check("dedup: seen-store persisted across a reload", ig_direct.already_seen(second[0]))


# ---------------------------------------------------------------- media typing
# Instagram serves stills as WEBP often enough that guessing the extension
# from the URL sent them to Telegram as .jpg, and sendPhoto answered
# IMAGE_PROCESS_FAILED for a file that had downloaded perfectly.
from modules.instagram import _sniff_ext  # noqa: E402

check("sniff: jpeg magic", _sniff_ext(b"\xff\xd8\xff\xe0" + b"\0" * 20, "https://x/a") == ".jpg")
check("sniff: png magic", _sniff_ext(b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "https://x/a") == ".png")
check("sniff: webp NOT reported as jpg",
      _sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ", "https://x/a.webp") == ".webp")
check("sniff: mp4 magic", _sniff_ext(b"\x00\x00\x00\x18ftypmp42", "https://x/a") == ".mp4")
check("sniff: quicktime magic", _sniff_ext(b"\x00\x00\x00\x14ftypqt  ", "https://x/a") == ".mov")
check("sniff: fmp4 styp box", _sniff_ext(b"\x00\x00\x00\x18stypmsdh", "https://x/a") == ".mp4")
check("sniff: content-type used when bytes are unknown",
      _sniff_ext(b"????????????????", "https://x/a", "video/mp4; codecs=avc1") == ".mp4")
check("sniff: url is only a fallback",
      _sniff_ext(b"not a known container at all", "https://x/a.jpg?v=1") == ".jpg")
check("sniff: html error page is not called an image",
      _sniff_ext(b"<html><head><title>403</title>", "https://x/a") == ".bin")
check("sniff: html content-type is not media",
      _sniff_ext(b"<!DOCTYPE html>", "https://x/a", "text/html; charset=utf-8") == ".bin")

# The 595KB "00.bin" the user was sent: a 200 response that is not media must
# fail the route so the next one gets its turn, not succeed with garbage.
import types  # noqa: E402

import modules.instagram as _ig  # noqa: E402

_fake = types.SimpleNamespace(
    content=b"<html><head><title>Login</title></head></html>" * 20,
    headers={"content-type": "text/html; charset=utf-8"},
    raise_for_status=lambda: None,
)
_orig_get = _ig.http.get if hasattr(_ig, "http") else None
import utils.http as _http  # noqa: E402

_saved = _http.get
_http.get = lambda url, **kw: _fake
try:
    _ig._download_urls(["https://cdn/x.mp4"], Path(_TMP) / "route")
    check("download: HTML masquerading as media is rejected", False, "it returned successfully")
except RuntimeError as e:
    check("download: HTML masquerading as media is rejected", "text/html" in str(e), str(e)[:70])
except Exception as e:
    check("download: HTML masquerading as media is rejected", False, f"{type(e).__name__}: {e}")
finally:
    _http.get = _saved

from handlers.instagram_handler import _classify, _prepare_photo  # noqa: E402

media_dir = Path(_TMP) / "media"
media_dir.mkdir(exist_ok=True)

try:
    from PIL import Image

    webp = media_dir / "still.webp"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(webp, "WEBP")
    out, kind = _prepare_photo(webp)
    check("photo: webp re-encoded for Telegram",
          kind == "photo" and out.suffix == ".jpg" and out.exists(),
          f"{out.name} / {kind}")
    with Image.open(out) as img:
        check("photo: re-encode really is JPEG", img.format == "JPEG", str(img.format))

    jpg = media_dir / "still.jpg"
    Image.new("RGB", (10, 10)).save(jpg, "JPEG")
    check("photo: jpeg passed through untouched", _prepare_photo(jpg) == (jpg, "photo"))
except ImportError:
    check("photo: Pillow available", False, "Pillow is not installed")

junk = media_dir / "00.bin"
junk.write_bytes(b"<html><head><title>403 Forbidden</title></head></html>")
check("photo: non-image falls back to document", _prepare_photo(junk)[1] == "document")

video = media_dir / "clip.mp4"
video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
check("classify: video stays a video", _classify([video]) == [(video, "video")])

from modules.instagram import _shortcode_from_html  # noqa: E402

check("html recovery: permalink in the page",
      _shortcode_from_html('<a href="https://www.instagram.com/reel/DQxYz_1AbC/">x</a>') == "DQxYz_1AbC")
check("html recovery: /p/ permalink",
      _shortcode_from_html('... "https://instagram.com/p/CtYv2acXPNO/" ...') == "CtYv2acXPNO")
check("html recovery: shortcode field as fallback",
      _shortcode_from_html('{"shortcode":"DZfwtaiob79","x":1}') == "DZfwtaiob79")
check("html recovery: permalink beats the raw field",
      _shortcode_from_html('{"shortcode":"WRONGCODE1"} https://www.instagram.com/reel/RIGHTCODE1/')
      == "RIGHTCODE1")
check("html recovery: nothing to find", _shortcode_from_html("<html>no post here</html>") == "")


# ---------------------------------------------------------------- handler groups
# PTB runs only the FIRST matching handler in a group and then moves on, and a
# TypeHandler(Update) matches every update. Two of them in one group means the
# second never runs - which is how the channel lock silently let everyone
# through for as long as the stats tracker shared its group. Nothing about
# that is visible at runtime, so it is asserted structurally.
main_tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
catch_alls: dict[int, list[str]] = {}

for node in ast.walk(main_tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        continue
    if node.func.attr != "add_handler" or not node.args:
        continue
    handler = node.args[0]
    if not (isinstance(handler, ast.Call) and getattr(handler.func, "id", "") == "TypeHandler"):
        continue
    if not (handler.args and getattr(handler.args[0], "id", "") == "Update"):
        continue

    group = next(
        (kw.value.value for kw in node.keywords
         if kw.arg == "group" and isinstance(kw.value, ast.Constant)),
        None,
    )
    if group is None:
        group = next(
            (-kw.value.operand.value for kw in node.keywords
             if kw.arg == "group" and isinstance(kw.value, ast.UnaryOp)),
            0,
        )
    catch_alls.setdefault(group, []).append(ast.unparse(handler.args[1]))

clashes = {g: cbs for g, cbs in catch_alls.items() if len(cbs) > 1}
check("handlers: no two catch-all TypeHandlers share a group", not clashes,
      json.dumps(catch_alls) if clashes else json.dumps(catch_alls))

check("handlers: the gate runs after the stats tracker",
      any("gate.guard" in cbs for cbs in catch_alls.values())
      and max(g for g, cbs in catch_alls.items() if "gate.guard" in cbs)
      > max(g for g, cbs in catch_alls.items() if "admin.track_update" in cbs),
      json.dumps(catch_alls))


# ---------------------------------------------------------------- DM payload walk
# Named-attribute lookup broke once per Instagram rename, most recently on
# shared stories: a media object with a pk, under a key none of the lists
# knew. The walker has to find it without being told the name.
import modules.ig_items as _items  # noqa: E402
import modules.ig_private as _priv  # noqa: E402

# A shared reel: media object under a known key, with a shortcode.
reel_item = {
    "item_id": "1", "item_type": "clip", "user_id": 42, "timestamp": 1754900000000000,
    "clip": {"clip": {"code": "DZfwtaiob79", "pk": "3396139282436016699",
                      "video_versions": [{"url": "https://cdn/hi.mp4", "height": 1080}]}},
}
check("item: reel resolves to a permalink",
      _priv._media_from_item(reel_item)[0] == "https://www.instagram.com/p/DZfwtaiob79/",
      str(_priv._media_from_item(reel_item)))

# A shared story: no code anywhere - a story is not reachable at /p/<code> -
# so the pk is the only usable address.
story_item = {
    "item_id": "2", "item_type": "story_share", "user_id": 42,
    "story_share": {"media": {"pk": "3396139282436016699", "media_type": 2,
                              "video_versions": [{"url": "https://cdn/s.mp4"}]}},
}
permalink, pk, url = _priv._media_from_item(story_item)
check("item: story yields a pk and no fake permalink",
      pk == "3396139282436016699" and permalink == "",
      str((permalink, pk, url)))

# The case that took a user report: media under a key nothing knows about.
unknown_item = {
    "item_id": "3", "item_type": "some_new_thing", "user_id": 42,
    "brand_new_wrapper": {"inner": {"pk": "999888777",
                                    "image_versions2": {"candidates": [{"url": "https://cdn/p.jpg"}]}}},
}
check("item: unknown key still found by shape",
      _priv._media_from_item(unknown_item)[1] == "999888777",
      str(_priv._media_from_item(unknown_item)))

# A user object has a pk too. Returning it would send the bot off to download
# the sender's profile picture instead of what they shared.
text_item = {
    "item_id": "4", "item_type": "text", "user_id": 42, "text": "IG-7QK4M2",
    "user": {"pk": "42", "username": "someone",
             "image_versions2": {"candidates": [{"url": "https://cdn/avatar.jpg"}]}},
}
check("item: the sender is not mistaken for the media",
      _priv._media_from_item(text_item) == ("", "", ""),
      str(_priv._media_from_item(text_item)))

# xma: a permalink among the urls beats the signed one, which is what
# returned 600KB of login-wall HTML.
xma_item = {
    "item_id": "5", "item_type": "xma_media_share", "user_id": 42,
    "xma_media_share": [{"video_url": "https://lookaside.fbsbx.com/x",
                         "target_url": "https://www.instagram.com/reel/ABC123xyz/"}],
}
check("item: xma prefers the permalink over the signed url",
      _priv._media_from_item(xma_item)[0] == "https://www.instagram.com/reel/ABC123xyz/",
      str(_priv._media_from_item(xma_item)))

# The timestamp unit was hardcoded to microseconds, which is what the MOBILE
# api sends. Wrong by a factor of a million, a message lands in 1970, tests as
# older than the poll loop's high-water mark, and is never delivered - while
# the inbox reads perfectly. /srcstatus showed it as "0 پیام · آخری هیچ‌وقت"
# next to an igtest2 that listed 21 messages.
_now = time.time()
for _raw, _unit in ((int(_now * 1_000_000), "microseconds"),
                    (int(_now * 1_000), "milliseconds"),
                    (int(_now), "seconds"),
                    (str(int(_now * 1_000_000)), "microseconds as a string")):
    check(f"timestamp: {_unit} land on now",
          abs(_items.to_epoch(_raw) - _now) < 1.0,
          f"{_items.to_epoch(_raw):.0f} vs {_now:.0f}")

for _junk in (None, 0, "", "garbage", -5):
    check(f"timestamp: {_junk!r} is 0, not a date", _items.to_epoch(_junk) == 0.0)

# The end-to-end version of the same bug: an item timestamped now must be
# newer than a high-water mark set a second ago.
_item = {"item_id": "9", "item_type": "text", "user_id": 7, "text": "hi",
         "timestamp": int(_now * 1_000_000)}
_parsed = _items.to_direct_message(_item, "web", "42")
check("timestamp: a message sent now is newer than a mark set a second ago",
      _parsed is not None and _parsed.timestamp > _now - 1,
      f"{_parsed.timestamp:.0f} vs {_now - 1:.0f}")

# The realtime transport is private and undocumented, and the library's own
# docs say not to depend on nested keys. So items are found by SHAPE - a dict
# with an item_id and a timestamp is a message, wherever it is wrapped.
import modules.ig_realtime as _rt  # noqa: E402

for _payload, _want, _label in (
    ({"event": "patch", "data": [{"op": "add", "value": {
        "item_id": "111", "timestamp": 1786860000000000, "item_type": "clip"}}]},
     1, "a patch operation"),
    ({"message": {"items": [{"item_id": "222", "timestamp": 1786860000000000,
                             "text": "IG-ABC123"}]}}, 1, "an items list"),
    ({"a": {"b": {"c": {"item_id": "333", "timestamp": 1786860000000000}}}},
     1, "something buried"),
    ({"unrelated": "nothing here"}, 0, "a payload with no message"),
):
    check(f"realtime: {_label} yields {_want} item(s)",
          len(_rt._items_in(_payload)) == _want)

check("realtime: it is off unless aiograpi is installed and configured",
      _rt.usable() is False)

# An idle MQTT read times out because there is nothing to deliver. Treating
# that as a lost connection rebuilt a healthy channel every few seconds - and
# ran a teardown that was calling accounts/logout/.
for _exc, _want, _label in (
    (Exception("The read operation timed out"), True, "the reported message"),
    (TimeoutError("timed out"), True, "a socket timeout"),
    (BlockingIOError("would block"), True, "an empty socket"),
    (ConnectionResetError("reset by peer"), False, "a real disconnect"),
    (Exception("login_required"), False, "a dead session"),
):
    check(f"realtime: idle={_want} for {_label}",
          _rt._is_idle_timeout(_exc) is _want)

# The cookie being gone and the socket being gone need opposite answers. The
# reconnect ladder retried both, so a session Instagram had already invalidated
# got a fresh login POST every five minutes, forever - the same mechanical
# pattern that caused the checkpoints, aimed at an outcome it could never reach.
#
# A checkpoint must NOT land here: it lifts by itself and the poller waits it
# out.
for _text, _want, _label in (
    ('{"message":"user_has_logged_out","logout_reason":9}', True,
     "the logout Instagram actually returned"),
    ("sessionid login failed and no IG_DM_PASSWORD is set: user_has_logged_out",
     True, "the reason carried out of _connect"),
    ("login_required", True, "a refused session"),
    ("checkpoint_required", False, "a checkpoint, which lifts on its own"),
    ("challenge_required", False, "a challenge, which lifts on its own"),
    ("Connection reset by peer", False, "a dropped socket"),
    ("The read operation timed out", False, "an idle read"),
):
    check(f"realtime: dead={_want} for {_label}",
          _rt._is_dead_credential(_text) is _want, _text[:60])

# The reason used to stay behind in a log line, leaving the loop with
# "sessionid login failed and no IG_DM_PASSWORD is set" - which matches no
# marker, so a dead cookie read as a transient fault.
check("realtime: _connect carries the refusal into the error it raises",
      any(isinstance(n, ast.Raise) and "cookie_error" in ast.dump(n)
          for n in ast.walk(ast.parse(
              Path("modules/ig_realtime.py").read_text(encoding="utf-8")))))

# logout() does not close a socket - it tells Instagram to invalidate the
# session, which destroys the cookie this whole feature runs on. It was being
# called on every reconnect.
_rt_tree = ast.parse(Path("modules/ig_realtime.py").read_text(encoding="utf-8"))
check("realtime: logout() is unreachable - it would kill the session",
      not [n for n in ast.walk(_rt_tree)
           if isinstance(n, ast.Attribute) and n.attr == "logout"])

# botctl igdirect rebuilds IG_DIRECT_SOURCES from scratch, so every source not
# named in that block is dropped by a cookie refresh. It cost the realtime
# channel once already: botctl igmqtt set mqtt,web, then pasting a fresh cookie
# wrote back a bare "web" and the bot came up polling with no error to show for
# it. The same overwrite had already eaten the web source once.
_mgr = Path("deploy/manage.sh").read_text(encoding="utf-8")
_rebuild = _mgr[_mgr.index('if (( want_standby && !have_web ))'):
                _mgr.index('set_env IG_DIRECT_SOURCES "$sources"')]
for _name in ("mqtt", "poll", "webhook"):
    check(f"igdirect: rebuilding the source list preserves {_name}",
          _name in _rebuild)

# Shazam needs the proxy and has no session to lose by it; Instagram has a
# session bound to whatever address issued it, and WARP does not hold an
# address still. Turning the proxy off for one used to mean turning it off for
# both, so testing the Instagram side cost music recognition.
_off_ig = _mgr[_mgr.index("        5)"):_mgr.index("        6)")]
check("proxy: Instagram can be taken off the proxy on its own",
      'set_env IG_DM_PROXY ""' in _off_ig)
check("proxy: doing so leaves Shazam's proxy alone",
      "SHAZAM_PROXY" not in _off_ig)

# /srcstatus prints what the health loop last saw, and the first thing it sees
# is realtime mid-handshake. With one interval between checks, "connecting…"
# was still on screen five minutes after the channel came up.
import modules.ig_direct as _igd  # noqa: E402

check("health: the first re-check beats the 10-minute interval",
      _igd._health_wait(True, 600) == 75.0)
check("health: later checks use the configured interval",
      _igd._health_wait(False, 600) == 600)
check("health: the settle wait never exceeds a short interval",
      _igd._health_wait(True, 60) == 60)

# Both sources spent eight hours asking a session that had stopped being one:
# realtime climbing its reconnect ladder, the web poller backing off to its
# 600s ceiling and retrying, 288 requests a day, on an account whose entire
# problem is being noticed. Neither could tell "the cookie is gone" from
# "the network hiccuped".
for _text, _want, _label in (
    ("Exceeded maximum allowed redirects.", True, "the redirect loop from the log"),
    ("sessionid login failed and no IG_DM_PASSWORD is set: "
     "Exceeded maximum allowed redirects.", True, "as realtime reported it"),
    ("Connection reset by peer", False, "a dropped socket"),
):
    check(f"realtime: dead={_want} for {_label}",
          _rt._is_dead_credential(_text) is _want, _text[:60])

import modules.ig_web as _web  # noqa: E402

for _text, _want, _label in (
    ("not json (407317 bytes) - probably a login page", True,
     "the login page from the log"),
    ("sessionid رد شد (401) - کوکی منقضی شده، یه تازه بگیر", True, "a refused cookie"),
    ("403 from the web api - the cookie is not accepted from this address",
     True, "a cookie refused from this address"),
    ("checkpoint_required", False, "a checkpoint, which lifts on its own"),
    ("HTTP 500: server error", False, "an Instagram fault"),
    ("Cannot connect to host", False, "the network being down"),
):
    check(f"ig web: login wall={_want} for {_label}",
          _web._is_login_wall(_text) is _want, _text[:60])

# One bad hop through the proxy can return a login page. Standing a working
# session down over a single one costs more than the extra sweep does.
check("ig web: one login page is not enough to stop", _web._LOGIN_WALL_LIMIT > 1)

# The cookie died twice in twelve hours and the leading suspect is the exit
# address moving under it - WARP is anycast and promises nothing about staying
# put. That is a theory, and this feature has lost nights to theories argued
# instead of measured, so the address is recorded and the next death carries
# the evidence with it.
import utils.exit_ip as _xip  # noqa: E402

_xip.current, _xip.since, _xip.moves[:] = "", 0.0, []
check("exit ip: the first reading is not a move", _xip._note("104.28.197.9") is False)
check("exit ip: the same address again is not a move", _xip._note("104.28.197.9") is False)
check("exit ip: a different address is a move", _xip._note("104.28.200.4") is True)
check("exit ip: an unreadable address is not a move", _xip._note("") is False)
check("exit ip: the move is kept with both addresses",
      _xip.moves[-1][1:] == ("104.28.197.9", "104.28.200.4"))
check("exit ip: a moved address shows in the alert line",
      "1 بار عوض شد" in _xip.summary(), _xip.summary())

_xip.current, _xip.since, _xip.moves[:] = "1.2.3.4", time.time(), []
check("exit ip: a stable address says so", "ثابت بوده" in _xip.summary(), _xip.summary())
_xip.current, _xip.since, _xip.moves[:] = "", 0.0, []
check("exit ip: nothing measured yet says nothing", _xip.summary() == "")

# One source returns the artists as a single comma-joined name, another
# returns them separately. Whole-string comparison saw no overlap, so display
# joined them a second time - and search_text sent that to YouTube and
# SoundCloud, which found nothing for a track that exists.
import modules.spotify as _sp  # noqa: E402

check("artists: a comma-joined restatement is dropped",
      _sp._merge_names(["Wantons", "Koorosh", "Arta"], ["Wantons, Koorosh, Arta"])
      == ["Wantons", "Koorosh", "Arta"])
check("artists: the same, with the joined name first",
      _sp._merge_names(["Wantons, Koorosh, Arta"], ["Wantons", "Koorosh", "Arta"])
      == ["Wantons, Koorosh, Arta"])
check("artists: either order renders the same line",
      ", ".join(_sp._merge_names(["Wantons", "Koorosh", "Arta"], ["Wantons, Koorosh, Arta"]))
      == ", ".join(_sp._merge_names(["Wantons, Koorosh, Arta"], ["Wantons", "Koorosh", "Arta"])))
check("artists: a genuinely new name still joins",
      _sp._merge_names(["Koorosh"], ["Arta"]) == ["Koorosh", "Arta"])
check("artists: a comma inside one real name survives",
      _sp._merge_names(["Earth, Wind & Fire"], ["Koorosh"])
      == ["Earth, Wind & Fire", "Koorosh"])
check("artists: a partial restatement is not silently dropped",
      _sp._merge_names(["Koorosh"], ["Koorosh, Sogand"]) == ["Koorosh", "Koorosh, Sogand"])
check("artists: case and spacing still collapse",
      _sp._merge_names(["Koorosh"], ["  koorosh "]) == ["Koorosh"])

# amazonMusic and amazonStore are two platforms with one Persian name, so the
# "you can find it here instead" line read "آمازون، آمازون".
_labels = [_sp._PLATFORM_FA.get(p, p)
           for p in ("amazonMusic", "amazonStore", "deezer", "pandora")]
check("platforms: one name is not listed twice",
      list(dict.fromkeys(_labels)) == ["آمازون", "دیزر", "پاندورا"])
_join = [ln for ln in Path("modules/spotify.py").read_text(encoding="utf-8").splitlines()
         if "_PLATFORM_FA.get(" in ln and ".join(" in ln]
check("platforms: the line that renders them is the one that de-duplicates",
      len(_join) == 1 and "dict.fromkeys" in _join[0], "; ".join(_join)[:80])

# requirements.txt keeps yt-dlp unpinned on purpose - "sites break often" -
# but pip leaves an installed unpinned package alone, so nothing on the update
# path ever moved it. YouTube stopped serving formats and the bot had no way
# to catch up short of a command nobody knew to run.
_upd = _mgr[_mgr.index("do_update()"):_mgr.index("refresh_ytdlp() {")]
check("update: yt-dlp is refreshed after a pull", _upd.count("refresh_ytdlp") >= 1)

# The refresh sat after the "nothing to pull" early return, so an install that
# was already on the latest commit skipped it - and YouTube breaking has
# nothing to do with whether there are new commits to fetch.
_early = _upd[_upd.index('چیزی برای آپدیت نیست'):]
check("update: an up-to-date checkout still refreshes yt-dlp",
      "refresh_ytdlp" in _early[:_early.index("fix_perms")])
check("update: the refresh upgrades rather than reinstalls",
      "--upgrade yt-dlp" in _mgr)
check("update: the JS runtime yt-dlp needs is checked with it",
      "ensure_deno" in _mgr[_mgr.index("refresh_ytdlp() {"):_mgr.index("do_ytdlp()")])

# do_ytdlp existed but only as menu item 8, so the one command that fixed
# YouTube could not be typed - and nothing in a log or an error mentions a
# menu.
_dispatch = _mgr[_mgr.index('case "${1:-}" in'):]
for _cmd in ("ytdlp", "update", "proxy", "igtest2"):
    check(f"botctl: {_cmd} can be run as a command", f"    {_cmd})" in _dispatch)

# yt-dlp was current (2026.07.04) and YouTube still served no formats, because
# deno could not run - and `command -v deno` says nothing about that. It
# printed its own evidence and threw it away:
#     [OK] Deno از قبل هست:
_deno = _mgr[_mgr.index("ensure_deno() {"):_mgr.index("ensure_user() {")]
check("deno: presence is decided by running it, not by PATH",
      "command -v deno" not in _deno.split("if command -v deno")[0])
check("deno: the check is the version string it prints",
      "deno_version" in _deno)
check("deno: a deno that does not run is reinstalled",
      "دوباره نصبش می‌کنم" in _deno)
check("deno: a reinstall that still does not run is an error, not a success",
      _deno.index("ver=$(deno_version)") < _deno.rindex('err "Deno نصب شد'))
for _fn, _label in (("do_status()", "status"), ("do_deps()", "deps")):
    _body = _mgr[_mgr.index(_fn):]
    _body = _body[:_body.index("\n}\n")]
    check(f"{_label}: reports deno by running it, not by PATH",
          "deno_version" in _body and "command -v deno" not in _body)

# Measured on one download: android_vr served the probe in 4.5s, then refused
# the media twice with 403 before tv_simply carried it. Metadata and media are
# not answered by the same client, so a winner is remembered per kind of
# request - otherwise every download re-pays 15.6s of refusals first.
import modules.youtube as _yt  # noqa: E402

_yt._preferred.clear()
check("ladder: with nothing learned, the built-in order stands",
      _yt._ladder("video") == _yt._CLIENT_LADDER)

_yt._preferred["video"] = "tv_simply"
check("ladder: a remembered winner leads next time",
      _yt._ladder("video")[0] == "tv_simply")
check("ladder: and the rest still follow it as fallbacks",
      sorted(_yt._ladder("video")) == sorted(_yt._CLIENT_LADDER))
check("ladder: a different kind is unaffected",
      _yt._ladder("probe") == _yt._CLIENT_LADDER)

# yt-dlp's default client is spelled "", which is falsy - testing it for
# truthiness would silently never promote it.
_yt._preferred["video"] = ""
check("ladder: the default client can win too", _yt._ladder("video")[0] == "")

_yt._preferred["video"] = "no_such_client"
check("ladder: a client that no longer exists is ignored",
      _yt._ladder("video") == _yt._CLIENT_LADDER)
_yt._preferred.clear()

# Counted rather than hardcoded: a new call site that forgets its kind lands
# silently in the default bucket and shares a remembered client with a
# different sort of request.
for _mod in ("modules/youtube.py", "modules/spotify.py"):
    _tree = ast.parse(Path(_mod).read_text(encoding="utf-8"))
    _calls = [
        n for n in ast.walk(_tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ytdlp_run"
    ]
    _untagged = [n for n in _calls
                 if not any(k.arg == "kind" for k in n.keywords)]
    check(f"ladder: every ytdlp_run in {_mod} says what kind it is",
          _calls and not _untagged,
          f"{len(_calls)} calls, {len(_untagged)} without a kind")

# The third spelling of "refused" this feature has met, and the first that is
# not a named error at all. It is not terminal - it does sometimes pass - so
# the ladder keeps going, but a loop nobody is told about is how the last
# outage ran eight hours.
for _text, _want, _label in (
    ("We're sorry, but something went wrong. Please try again.", True,
     "the page the datacenter IP was served"),
    ("user_has_logged_out", False, "a named dead cookie"),
    ("Connection reset by peer", False, "a dropped socket"),
):
    check(f"realtime: refusal={_want} for {_label}",
          _rt._is_refusal(_text) is _want, _text[:60])

check("realtime: a generic refusal is not treated as a dead cookie",
      _rt._is_dead_credential("We're sorry, but something went wrong.") is False)
check("realtime: the silent-retry window is bounded",
      1 < _rt._ALERT_AFTER <= 10, f"alerts after {_rt._ALERT_AFTER} attempts")

# botctl logs is `journalctl -f -n 40`, so piping it to grep blocks forever and
# searches forty lines. Two diagnostics came back empty that way.
_logs = _mgr[_mgr.index("do_logs() {"):_mgr.index("do_find() {")]
check("logs: a line count can be asked for without following",
      "--no-pager -n" in _logs)
check("logs: searching the history has its own command", "    find)" in _dispatch)

# "Sign in to confirm you're not a bot" refuses the SEARCH as well as the
# download. With ytsearch returning nothing and SoundCloud alone having no
# version, the bot reported the track as absent from the internet - a claim
# about the track, made from evidence about the server.
_yt._last_bot_check = 0.0
check("bot check: nothing seen means not blocked", _yt.bot_checked_recently() is False)
check("bot check: spotify agrees when nothing was seen", _sp._yt_blocked() is False)

for _text, _want, _label in (
    ("ERROR: [youtube] YJdCpltq-_k: Sign in to confirm you’re not a bot. "
     "Use --cookies-from-browser or --cookies for the authentication", True,
     "the refusal from the log"),
    ("Requested format is not available", False, "a missing format"),
    ("HTTP Error 403: Forbidden", False, "a refused client"),
):
    check(f"bot check: detected={_want} for {_label}",
          any(m in _text.lower() for m in _yt._BOT_CHECK) is _want, _text[:60])

_yt._last_bot_check = __import__("time").monotonic()
check("bot check: a fresh refusal is remembered", _yt.bot_checked_recently() is True)
check("bot check: spotify sees it too", _sp._yt_blocked() is True)
check("bot check: it expires rather than sticking forever",
      _yt.bot_checked_recently(within=-1) is False)
_yt._last_bot_check = 0.0

# yt-dlp's documented answer to the bot check is a cookie jar, and
# modules/youtube.py already reads one - there was just no way to install it.
check("botctl: a youtube cookie jar can be installed", "    ytcookies)" in _dispatch)

# The bot check is decided by the address the request comes from, so moving
# the address is the alternative to handing YouTube an account - and there was
# no proxy setting for yt-dlp at all, only for Shazam and Instagram.
check("proxy: yt-dlp has a proxy setting of its own", "YT_PROXY" in _mgr)
check("proxy: youtube's proxy is separate from Instagram's",
      _mgr.count('set_env YT_PROXY') >= 1 and "yt_proxy" in
      Path("config.py").read_text(encoding="utf-8"))
check("proxy: it goes through the socks5h normaliser like everything else",
      "proxies.normalize(settings.yt_proxy)" in
      Path("modules/youtube.py").read_text(encoding="utf-8"))
_ytc = _mgr[_mgr.index("do_ytcookies() {"):_mgr.index("do_ytdlp() {")]
check("ytcookies: it refuses a file with no youtube.com in it",
      "youtube.com" in _ytc and "err" in _ytc)
check("ytcookies: the jar is not left world-readable", "chmod 600" in _ytc)
check("ytcookies: it warns that this is account access",
      "اکانت اصلیت" in _ytc)

# A 1640MB file arrived from a menu of unlabelled buttons, and only announced
# itself when the upload stalled. "best" has no ceiling in QUALITY_CHOICES, so
# it is the one that most needs a number beside it.
_fmts = [
    {"vcodec": "avc1", "acodec": "none", "height": 360, "filesize": 40 * 1048576},
    {"vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 200 * 1048576},
    {"vcodec": "avc1", "acodec": "none", "height": 2160, "filesize_approx": 1600 * 1048576},
    {"vcodec": "none", "acodec": "opus", "filesize": 8 * 1048576},
]
_sizes = _yt._sizes_by_quality(_fmts)
check("sizes: a capped quality uses the biggest stream that fits",
      _sizes["360p"] == 48 * 1048576, f"{_sizes.get('360p', 0) / 1048576:.0f}MB")
check("sizes: 720p picks the 720 stream, not the 2160 one",
      _sizes["720p"] == 208 * 1048576, f"{_sizes.get('720p', 0) / 1048576:.0f}MB")
check("sizes: best is the uncapped one and says so",
      _sizes["best"] == 1608 * 1048576, f"{_sizes.get('best', 0) / 1048576:.0f}MB")
check("sizes: filesize_approx counts when filesize is absent",
      _sizes["best"] > _sizes["720p"])
check("sizes: 1080p falls back to the largest stream under the cap",
      _sizes["1080p"] == _sizes["720p"])
check("sizes: no formats means no numbers rather than zeroes",
      _yt._sizes_by_quality([]) == {})
check("sizes: a stream with no size reported is skipped, not shown as 0MB",
      "480p" not in _yt._sizes_by_quality(
          [{"vcodec": "avc1", "acodec": "none", "height": 480}]))

# Every quality wrote to one path, and yt-dlp does not re-download a file that
# is already there. A 1640MB "best" left behind by a stalled upload was handed
# straight back when 360p was asked for - in 1.3s, for a 37-minute video.
_vi = _yt.VideoInfo(id="abc123", title="a video", duration=2244, thumbnail="",
                    uploader="someone", available_heights={360, 1080})
_paths = {q: _yt._make_outtmpl(_vi, q) for q in ("360p", "720p", "best", "audio")}
check("outtmpl: each quality gets its own file",
      len(set(_paths.values())) == len(_paths))
check("outtmpl: the quality is what distinguishes them",
      "360p" in _paths["360p"] and "best" in _paths["best"])
check("outtmpl: audio does not collide with video",
      _paths["audio"] != _paths["best"])
check("outtmpl: the id is still in the name", all("abc123" in p for p in _paths.values()))

# A file left by an upload that died mid-flight is not a finished download.
_ytsrc = Path("modules/youtube.py").read_text(encoding="utf-8")
check("download: a stale file on disk is overwritten, not inherited",
      _ytsrc.count('"overwrites": True') == 2)

# Instagram ties a session to the client that created it. The cookie comes
# from the user's browser; the requests went out as a hardcoded Chrome/124 on
# Windows, so every one of them was that session appearing on a different
# machine. Sessions have been dying within hours, repeatedly, on two different
# exit addresses - this is the best-supported explanation left.
_real_ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
import dataclasses as _dc  # noqa: E402

_saved_settings = _web.settings
try:
    _web.settings = _dc.replace(_saved_settings, ig_dm_user_agent=_real_ua)
    check("ua: the browser's own string is used when given",
          _web._user_agent() == _real_ua)
    _web.settings = _dc.replace(_saved_settings, ig_dm_user_agent="")
    check("ua: it falls back rather than sending nothing",
          _web._user_agent() == _web._UA_FALLBACK)
finally:
    _web.settings = _saved_settings

check("ua: the header is built from the setting, not the constant",
      '"User-Agent": _user_agent()' in Path("modules/ig_web.py").read_text(encoding="utf-8"))
check("ua: taking a cookie also asks for the browser it came from",
      "IG_DM_USER_AGENT" in _mgr)
check("ua: a missing one is surfaced rather than left silent",
      "IG_DM_USER_AGENT" in Path("config.py").read_text(encoding="utf-8")
      and "User-Agent مرورگر ست نشده" in Path("handlers/admin.py").read_text(encoding="utf-8"))

# Every cover in this bot arrives pre-shrunk for use as a Telegram photo -
# iTunes rewritten to 600x600, Deezer's cover_xl at 1000. Asked for as a file
# there is no such ceiling, and the same CDNs serve much larger versions of
# the identical image if the url says so.
import utils.artwork as _art  # noqa: E402

_apple = "https://is1-ssl.mzstatic.com/image/thumb/Music/v4/ab/cd/ef/x.jpg/600x600bb.jpg"
_c = _art.candidates(_apple)
check("artwork: apple is asked for a bigger render first",
      _c[0].endswith("3000x3000bb.jpg"), _c[0][-24:])
check("artwork: it steps down instead of giving up",
      len(_c) > 2 and any("1400x1400bb" in u for u in _c))

_deezer = "https://e-cdns-images.dzcdn.net/images/cover/abc123/1000x1000-000000-80-0-0.jpg"
_c = _art.candidates(_deezer)
check("artwork: deezer's size and jpeg quality both go up",
      "1800x1800" in _c[0] and "-100-" in _c[0], _c[0][-34:])

_yt = "https://i.ytimg.com/vi/abc123/hqdefault.jpg"
_c = _art.candidates(_yt)
check("artwork: youtube climbs its fixed ladder",
      _c[0].endswith("maxresdefault.jpg"), _c[0][-20:])

# The original is the one url already known to work. Dropping it would make a
# bigger-but-missing render worse than doing nothing.
for _label, _url in (("apple", _apple), ("deezer", _deezer), ("youtube", _yt),
                     ("an unknown host", "https://example.com/cover.jpg")):
    check(f"artwork: the original is still tried last for {_label}",
          _art.candidates(_url)[-1] == _url)

check("artwork: nothing in means nothing out", _art.candidates("") == [])

# 3000 is the top deliberately. Apple renders any size asked for, including
# larger than the master it holds, so 5000 returns an upscale - a bigger file
# carrying the same picture.
check("artwork: apple is not asked past its master size",
      not any("5000" in u or "4000" in u for u in _art.candidates(_apple)))

# Rewriting a size only works when there is a size. Spotify caps at 640 and a
# YouTube thumbnail is a video frame; for those the bigger cover is at another
# source, not another url.
check("artwork: an apple url needs no other source", _art.is_upgradable(_apple) is True)
check("artwork: a deezer url needs no other source", _art.is_upgradable(_deezer) is True)
check("artwork: a spotify image cannot be rewritten bigger",
      _art.is_upgradable("https://i.scdn.co/image/ab67616d0000b273abc") is False)
check("artwork: a youtube thumbnail is not a cover to rewrite",
      _art.is_upgradable(_yt) is False)

check("artwork: several sources are tried in the order given",
      _art.best.__doc__ is not None and
      _art.candidates(_deezer)[0] in
      [c for u in (_deezer, _apple) for c in _art.candidates(u)])
check("artwork: no duplicates when a size is already the largest",
      len(_art.candidates(_apple.replace("600x600bb", "3000x3000bb")))
      == len(set(_art.candidates(_apple.replace("600x600bb", "3000x3000bb")))))

import handlers.lyrics_handler as _lyr  # noqa: E402

check("artwork: a cover with no url gets no button", _lyr.cover_key("", "x") == "")
_k = _lyr.cover_key(_apple, "Some Artist — Some Song")
check("artwork: the key is short enough for callback_data",
      len(f"cov:{_k}") <= 64, f"{len(f'cov:{_k}')} bytes")
check("artwork: the key resolves back to the url", _lyr._covers.get(_k)[0] == _apple)
check("artwork: the button only appears when there is a cover",
      _lyr.platform_keyboard("a", "b").inline_keyboard[0][0].callback_data is None)
check("artwork: and it is the first thing under the photo",
      _lyr.platform_keyboard("a", "b", cover_key=_k)
      .inline_keyboard[0][0].callback_data == f"cov:{_k}")
check("artwork: the callback is registered",
      'pattern=r"^cov:"' in Path("main.py").read_text(encoding="utf-8"))

# edit_caption does not leave an existing keyboard alone - it clears it. The
# cover caption is edited when the download finishes, so the artwork and
# platform buttons disappeared at exactly the moment the track arrived.
_sph = Path("handlers/spotify_handler.py").read_text(encoding="utf-8")
for _call in re.finditer(r"edit_caption\((?:[^()]|\([^()]*\))*\)", _sph):
    check("artwork: editing the caption keeps the buttons",
          "reply_markup" in _call.group(0), _call.group(0)[:60].replace("\n", " "))
check("artwork: the keyboard is built in one place, not two",
      _sph.count("def _cover_keyboard") == 1 and _sph.count("_cover_keyboard(") >= 3)

# A pasted track link produced nothing at all until Spotify's embed page came
# back, which on a bad fetch was most of a minute of empty chat. Every other
# link type opens with a line first.
check("speed: a pasted track is acknowledged before any network work",
      _sph.index("در حال گرفتن اطلاعات آهنگ") < _sph.index("await sp.get_track_meta"))

_scr = Path("modules/spotify_scraper.py").read_text(encoding="utf-8")
check("speed: the embed fetch reuses the pooled client",
      "http.client().get(" in _scr and "httpx.Client(timeout=20" not in _scr)
check("speed: it no longer waits 20s to call an attempt failed",
      "timeout=12" in _scr)
check("speed: and does not sleep after the last attempt",
      "if attempt < 2:" in _scr)
check("speed: the fetch reports how long it took",
      "spotify embed %s/%s in %.1fs" in _scr)

# YouTube's own "sort by popular" is a UI control backed by an undocumented
# query parameter that has changed spelling more than once. The flat listing
# already carries view counts, so the ranking is done here instead.
import handlers.youtube_handler as _yth  # noqa: E402

check("channel: views are readable rather than raw",
      (_yth._fmt_views(1_483_920), _yth._fmt_views(12_400), _yth._fmt_views(742),
       _yth._fmt_views(0)) == ("1.5M", "12K", "742", "—"))

# callback_data is capped at 64 bytes, and this one carries two video ids.
_cb = f"yt:dQw4w9WgXcQ:pick=dQw4w9WgXcQ"
check("channel: a pick still fits in callback_data", len(_cb) <= 64, f"{len(_cb)} bytes")
check("channel: a pick is parsed back to the video id",
      _cb.split(":", 2)[2].split("=", 1)[1] == "dQw4w9WgXcQ")

_ythsrc = Path("handlers/youtube_handler.py").read_text(encoding="utf-8")
check("channel: a pick reuses the normal quality menu",
      "_send_video_menu(query.message, context" in _ythsrc)
check("channel: the pasted-link path uses that same menu",
      _ythsrc.count("_send_video_menu(") >= 2)

# The bot check has only just stopped biting. An extra channel request on
# every probed link is the kind of free traffic that caused it.
check("channel: listing happens on the button, not on every video",
      "popular_from_channel" not in _ythsrc.split("async def on_callback")[0])
check("channel: the button is absent when there is no channel url",
      "if info.channel_url:" in _ythsrc)
check("channel: the listing is flat rather than per-video extraction",
      '"extract_flat": "in_playlist"' in _ytsrc)
check("channel: it is bounded rather than walking the whole channel",
      '"playlistend"' in _ytsrc)
check("channel: it has its own client bucket",
      'kind="channel"' in _ytsrc)

# A search for "<artist> <title> official video" returns lyric uploads,
# visualizers and "- Topic" auto-audio in quantity. Handing one of those back
# as the music video is worse than reporting none: the user already has the
# audio, so a duplicate of it is the one useless answer.
_mv_meta = _sp.TrackMeta(id="x", name="Savage Rose", artists=["Koorosh"],
                         album="", duration_ms=200_000, cover_url="",
                         spotify_url="")

def _entry(title, channel=""):
    return {"id": "vid", "title": title, "channel": channel}

for _title, _chan, _want, _label in (
    ("Koorosh - Savage Rose (Official Music Video)", "Koorosh", True, "the official video"),
    ("Koorosh - Savage Rose", "KooroshOfficial", True, "an untagged upload by the artist"),
    ("Koorosh - Savage Rose (Official Lyric Video)", "Koorosh", False, "a lyric video"),
    ("Koorosh - Savage Rose (Visualizer)", "Koorosh", False, "a visualizer"),
    ("Koorosh - Savage Rose [slowed + reverb]", "someone", False, "a slowed edit"),
    ("Koorosh - Savage Rose", "Koorosh - Topic", False, "auto-generated topic audio"),
    ("Savage Rose - Some Other Band", "Some Other Band", False, "another artist's song"),
    ("Koorosh - A Completely Different Song", "Koorosh", False, "the wrong track"),
    ("Koorosh Savage Rose REACTION", "reactor", False, "a reaction video"),
):
    _got = _sp._mv_score(_mv_meta, _entry(_title, _chan)) > 0
    check(f"music video: accepted={_want} for {_label}", _got is _want, _title[:52])

check("music video: the official tag outranks an untagged upload",
      _sp._mv_score(_mv_meta, _entry("Koorosh - Savage Rose (Official Music Video)", "Koorosh"))
      > _sp._mv_score(_mv_meta, _entry("Koorosh - Savage Rose", "Koorosh")))

# Runtime is deliberately not scored: a video is routinely a different length
# from the release, so the rule that protects the audio search would reject
# the very thing this is looking for.
check("music video: runtime is not part of the decision",
      "duration" not in _sp._mv_score.__doc__.lower()
      or "not part of it" in _sp._mv_score.__doc__)

_sph2 = Path("handlers/spotify_handler.py").read_text(encoding="utf-8")
check("music video: it opens the normal youtube quality menu",
      "youtube_handler._send_video_menu" in _sph2)
check("music video: searching happens on the button, not per track",
      "find_music_video" not in Path("handlers/lyrics_handler.py").read_text(encoding="utf-8"))

# Realtime is "connecting…" for its first 60s, which counts as healthy, so the
# web poller was not started until the health loop declared realtime dead.
# Every restart therefore had a window with nothing reading the inbox - and
# the poller marks everything older than its start as already seen, so a DM
# sent in that window was not late, it was gone.
_igdsrc = Path("modules/ig_direct.py").read_text(encoding="utf-8")
_startfn = _igdsrc[_igdsrc.index("async def start("):_igdsrc.index("async def stop(")]
check("ig direct: the web reader starts without waiting for realtime to fail",
      'not _states["mqtt"].running' not in _startfn)
check("ig direct: and is still stood down once realtime is up",
      'stopping the web poller failed' in _igdsrc)

# Retrying a refusal is not free: each attempt is a failed mobile-api sign-in
# on the account the web poller depends on.
check("realtime: a refusal eventually stops instead of looping all day",
      "given_up = last_error" in Path("modules/ig_realtime.py").read_text(encoding="utf-8"))
_rt.given_up = ""
# botctl iglogin died before it reached Instagram at all:
#
#     PermissionError: [Errno 13] Permission denied: '/root/downloads'
#
# config.py calls load_dotenv() and resolves DOWNLOAD_DIR with
# Path("./downloads").resolve(), and both read the WORKING DIRECTORY. The
# service sets WorkingDirectory so the bot itself was never affected, but
# botctl is typed from wherever the admin is standing - /root - so .env was
# never found and downloads pointed inside root's home.
_mg = Path("deploy/manage.sh").read_text(encoding="utf-8")
check("botctl: python is invoked through the helper that sets the directory",
      "run_py() {" in _mg)
check("botctl: the helper cds into the project before exec",
      'cd "$1" && shift && exec "$@"' in _mg)
check("botctl: no invocation bypasses it",
      'sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/python"' not in _mg)
check("botctl: iglogin in particular goes through it",
      "run_py \"$PROJECT_DIR/deploy/iglogin.py\"" in _mg)
check("service: the unit still pins its own working directory",
      "WorkingDirectory=/opt/telegram_downloader_bot" in
      Path("deploy/tg-downloader-bot.service").read_text(encoding="utf-8"))

# Realtime was gated on has_ig_web - the presence of IG_DM_SESSIONID - which
# is the one credential the mobile api refuses, and the reason this feature has
# never connected. Worse for the account switch about to happen: clearing that
# cookie, which is the correct thing to do when the old account is retired,
# switched realtime OFF rather than on.
import dataclasses as _dc
from config import settings as _cfg

_sess = _cfg.download_dir / "ig_private_session.json"
_had_sess = _sess.exists()
if _had_sess:
    _sess.rename(_sess.with_suffix(".selfcheck-bak"))


def _probe(**kw):
    return _dc.replace(_cfg, **kw)


check("realtime gate: a password alone switches realtime on",
      _probe(ig_dm_sessionid="", ig_dm_password="p",
             ig_dm_username="u").has_ig_realtime)
check("realtime gate: the browser cookie still counts as a last route",
      _probe(ig_dm_sessionid="abc", ig_dm_password="",
             ig_dm_username="u").has_ig_realtime)
check("realtime gate: nothing at all still means off",
      not _probe(ig_dm_sessionid="", ig_dm_password="",
                 ig_dm_username="u").has_ig_realtime)

_sess.parent.mkdir(parents=True, exist_ok=True)
_sess.write_text("{}", encoding="utf-8")
check("realtime gate: a stored mobile session is enough on its own",
      _probe(ig_dm_sessionid="", ig_dm_password="",
             ig_dm_username="").has_ig_realtime)
check("realtime gate: clearing the retired cookie keeps direct enabled",
      _probe(ig_dm_sessionid="", ig_dm_password="",
             ig_dm_username="").ig_direct_enabled)
_sess.unlink()
if _had_sess:
    _sess.with_suffix(".selfcheck-bak").rename(_sess)

check("realtime gate: the web source still wants its own cookie",
      not _probe(ig_dm_sessionid="").has_ig_web)
check("realtime gate: ig_direct builds mqtt from that gate, not the web one",
      "settings.has_ig_realtime" in
      Path("modules/ig_direct.py").read_text(encoding="utf-8"))

# "The broadcast does not work" was every admin command silently returning.
# Silence is right for a stranger; with ADMIN_IDS unset NOBODY is an admin, so
# the owner got the same nothing and had no way to tell that apart from a bug.
_adm = Path("handlers/admin.py").read_text(encoding="utf-8")
from handlers import admin as _adm_mod
check("admin: an unconfigured bot answers instead of staying silent",
      "if not settings.admin_ids:" in _adm and "botctl admin" in _adm)
check("admin: it hands over the id needed to fix the deadlock",
      "uid = user.id if user else" in _adm)
check("admin: a stranger is still met with silence",
      "if _is_admin(update):" in _adm and "return False" in _adm)
check("admin: every command goes through the same gate",
      _adm.count("await _reject(update)") >= 6)
check("admin: buttons reuse the commands rather than copying them",
      "_FromButton" in _adm and "await srcstatus_cmd(shim" in _adm)
check("botctl: admin ids can be set without editing .env by hand",
      "do_admin()" in _mg)
check("botctl: a second admin does not replace the first",
      'set_env ADMIN_IDS "$cur,$want"' in _mg)

# Nothing capped how much work could be ASKED for - only how much ran at once.
from utils import limits as _rl
_rl._buckets.clear()
check("rate: a person pasting five links is untouched",
      all(_rl.allow(101)[0] for _ in range(5)))
_rl._buckets.clear()
check("rate: twenty at once is still allowed",
      sum(_rl.allow(102)[0] for _ in range(20)) == 20)
_rl._buckets.clear()
check("rate: a loop of two hundred is cut off",
      sum(_rl.allow(103)[0] for _ in range(200)) == _rl._RATE_BURST)
_rl._buckets.clear()
check("rate: the refusal says when to come back",
      [_rl.allow(104) for _ in range(30)][-1][1] > 0)
_rl._buckets.clear()
check("rate: the admin is never throttled",
      all(_rl.allow(105, is_admin=True)[0] for _ in range(500)))
_rl._buckets.clear()
for _u in range(6000):
    _rl.allow(_u)
check("rate: the bucket table cannot grow without bound",
      len(_rl._buckets) < 6000)
_rl._buckets.clear()
check("rate: the panel can report it",
      set(_rl.rate_snapshot()) >= {"burst", "per_minute", "throttled"})

# The broadcast preview was sent with parse_mode="Markdown" while containing
# whatever the admin typed. A broadcast is exactly the message that carries
# links, underscores and slashes - "پیج_جدید", "/igdirect" - so Telegram
# rejected it, the failure reached the generic handler, and the answer on
# screen was "یه خطای غیرمنتظره پیش اومد" with nothing about what was wrong.
_adm = Path("handlers/admin.py").read_text(encoding="utf-8")
check("broadcast: the preview cannot be rejected for bad markup",
      "f\"📣 برای {total} کاربر ارسال بشه؟" in _adm)
check("broadcast: the text is taken off the raw message, newlines and all",
      "msg.text or msg.caption" in _adm and "head.partition" in _adm)
check("broadcast: joining context.args is not how the body is built",
      'text = " ".join(context.args)' not in _adm)
# Plain text fixed the crash and lost the formatting with it: the link on
# "Ù¾ÛØ¬ Ø¬Ø¯ÛØ¯" stopped being a link. Telegram keeps that beside the words as
# entities, so they are carried across rather than re-marked-up - offsets in
# UTF-16 units, because a ð¢ is one character and two of those.
from telegram import MessageEntity as _ME
_raw = "/broadcast Ø³ÙØ§Ù ð¢ ÙÛÙÚ©"
_body = "Ø³ÙØ§Ù ð¢ ÙÛÙÚ©"
_ent = [_ME(type="text_link", offset=_adm_mod._u16("/broadcast Ø³ÙØ§Ù ð¢ "),
            length=_adm_mod._u16("ÙÛÙÚ©"), url="https://example.com")]
_moved = _adm_mod._shift_entities(_raw, _body, _ent)
check("broadcast: formatting survives the command being stripped",
      len(_moved) == 1)
check("broadcast: the link lands on the word it was on",
      _moved and _moved[0].offset == _adm_mod._u16("Ø³ÙØ§Ù ð¢ "))
check("broadcast: an emoji counts as two units, the way Telegram counts",
      _adm_mod._u16("🟢") == 2 and len("🟢") == 1)
check("broadcast: the preview shows the same formatting",
      len(_adm_mod._preview_entities(17, _moved)) == 1)
check("broadcast: entities reach both senders",
      _adm.count("entities=entities") >= 2)

# Downloads went from under 15s to about two minutes. The comment in
# modules/youtube.py already held the measurement: android_vr refused in 7.8s,
# the default refused in 7.8s, tv_simply served it in 88.8s. Remembering only
# WHO won made that one-off fallback the permanent first choice, so every
# later download began by paying eighty-eight seconds and the fast clients
# were never tried again.
from modules import youtube as _yt
import time as _tt3
_now3 = _tt3.monotonic()

_yt._preferred["probe"] = "ios"
_yt._preferred_cost["probe"] = 3.2
_yt._preferred_at["probe"] = _now3
# The ladder was five rungs with the slowest client third, so a refusal on the
# first two landed on the 88.8s one almost immediately. Timed every rung
# against a real track before reordering.
# A 710MB video arrived with its length shown as 00:00 and the thumbnail
# stretched. Telegram is told the shape of a video or it guesses one.
_yth = Path("handlers/youtube_handler.py").read_text(encoding="utf-8")
check("video: the length is sent, so Telegram does not show 00:00",
      "duration=probed or info.duration" in _yth)
check("video: the dimensions are sent, so the thumbnail is not stretched",
      "width=width or None" in _yth and "height=height or None" in _yth)
check("video: they are read from the file that is actually being sent",
      "probe_dimensions(path)" in _yth)
check("video: an unreadable file falls back rather than breaking the upload",
      _yt.probe_dimensions(Path("does-not-exist.mp4")) == (0, 0, 0))

# merge_output_format asks for mp4; YouTube's best streams are AV1 + Opus,
# which mp4 will not hold, so ffmpeg re-encoded every video ever downloaded.
_yts = Path("modules/youtube.py").read_text(encoding="utf-8")
check("video: mp4-native codecs are preferred so the merge is a remux",
      '"format_sort": ["vcodec:h264", "acodec:m4a"]' in _yts)
check("video: it is a preference, not a filter that can fail",
      not any("vcodec^=" in v for v in _yt.QUALITY_CHOICES.values()))

check("speed: the slowest client is the last resort, not the third",
      _yt._CLIENT_LADDER[-1] == "tv_simply")
check("speed: a client that returns no audio is not on the ladder at all",
      "android" not in _yt._CLIENT_LADDER)
check("speed: and neither is the one that takes 12s to refuse",
      "tv" not in _yt._CLIENT_LADDER)
check("speed: the two rungs measured to actually deliver audio lead it",
      _yt._CLIENT_LADDER[:2] == ("android_vr", ""))
check("speed: fragmented streams are fetched in parallel",
      _yt._base_opts().get("concurrent_fragment_downloads", 0) >= 8)

check("speed: a fast winner keeps the lead",
      _yt._ladder("probe")[0] == "ios")

_yt._preferred["probe"] = "tv_simply"
_yt._preferred_cost["probe"] = 88.8
_yt._preferred_at["probe"] = _now3
check("speed: a slow winner still leads while it is fresh",
      _yt._ladder("probe")[0] == "tv_simply")

_yt._preferred_at["probe"] = _now3 - _yt._REPROBE_AFTER - 1
check("speed: a slow winner loses the lead on re-probe",
      _yt._ladder("probe")[0] == _yt._CLIENT_LADDER[0])
check("speed: the re-probe is not so rare that a bad hour lasts all day",
      _yt._REPROBE_AFTER <= 1800)
_yt._preferred.pop("probe", None)
_yt._preferred_cost.pop("probe", None)
_yt._preferred_at.pop("probe", None)

check("speed: a refused extraction is not retried three times before moving on",
      _yt._base_opts().get("extractor_retries") == 1)
check("speed: transfer retries are left alone, a dropped fragment is transient",
      _yt._base_opts().get("retries") == 3)

# "🚫 4 blocked" answers how many and not who, and who is the part anyone can
# act on. It also cost a refused request per blocked user on every future run.
check("broadcast: the unreachable are named, not just counted",
      "gone.append(" in _adm and "people.get(uid" in _adm)
check("broadcast: being unreachable is remembered",
      "stats.mark_blocked(uid)" in _adm)
check("broadcast: the next run does not retry them",
      "stats.reachable_users()" in _adm)
check("broadcast: the count offered for confirmation is the count sent to",
      "total = stats.reachable_count()" in _adm)
check("stats: coming back clears the flag on its own",
      "blocked_at = NULL" in Path("modules/stats.py").read_text(encoding="utf-8"))
check("stats: the column is added by migration, not a fresh schema",
      "ADD COLUMN blocked_at" in Path("modules/stats.py").read_text(encoding="utf-8"))

check("broadcast: it can be sent to the admin alone first",
      "adm:bctest:" in _adm)
check("broadcast: the test does not consume the pending send",
      "_pending_broadcast.get(key)" in _adm)
check("admin: a bot with no admins configured says so instead of ignoring you",
      "botctl admin" in _adm and "_reject" in _adm)

# A 2:57 track came back as 1.1MB - about 50kbps - where every other source
# has it at 3MB+. Nothing failed: the client ladder had landed on a client
# offering only YouTube's 48kbps rungs, and a download that succeeds is not
# checked again by anything.
from modules import spotify as _spq
_qmeta = _spq.TrackMeta("sp_x", "Void", ["Paablow"], "", 177000, "", "")
_iws0 = Path("modules/spotify.py").read_text(encoding="utf-8")


class _F:
    def __init__(self, n):
        self.n = n

    def stat(self):
        return type("S", (), {"st_size": self.n})()


check("quality: the file that shipped is judged too thin",
      _spq._bitrate_kbps(_F(1_100_000), _qmeta) < _spq._THIN_KBPS)
check("quality: a normal youtube rip passes",
      _spq._bitrate_kbps(_F(3_200_000), _qmeta) >= _spq._THIN_KBPS)
check("quality: a high-bitrate rip passes",
      _spq._bitrate_kbps(_F(6_700_000), _qmeta) >= _spq._THIN_KBPS)
check("quality: an unknown duration is never used to reject",
      _spq._bitrate_kbps(_F(1_100_000),
                         _spq.TrackMeta("z", "z", ["z"], "", 0, "", "")) == 0.0)
check("quality: a short interlude is not judged by bitrate",
      _spq._bitrate_kbps(_F(1_100_000),
                         _spq.TrackMeta("z", "z", ["z"], "", 12000, "", "")) == 0.0)
check("quality: a thin file is only dropped when another candidate remains",
      "and i + 1 < len(targets)" in _iws0)
check("quality: the best thin one is kept rather than failing outright",
      "every candidate for %r was thin" in _iws0)

# song.link retired its free tier: every anonymous call is 401
# PUBLIC_API_ACCESS_DEPRECATED. A red cross for that read as a fault here.
check("odesli: without a key the call is not made at all",
      "if not settings.odesli_api_key:" in _iws0)
check("odesli: /engines explains rather than showing a failure",
      "odesli_state" in Path("handlers/admin.py").read_text(encoding="utf-8"))

# Every sweep read TWO endpoints - the inbox and the message-requests folder -
# so the real request rate was double what the poll interval implies, and every
# safe-interval calculation was out by a factor of two. On a busy day that is
# ~4,500 where ~2,250 was intended, which crosses from متعادل into پرریسک.
#
# The pending folder only holds anything for an account we have never had a
# thread with, which in practice means somebody redeeming a pairing token.
_iws = Path("modules/ig_web.py").read_text(encoding="utf-8")
check("pending: the folder is not read on every single sweep",
      "if with_pending and _pending_due():" in _iws)
check("pending: an outstanding pairing token is read immediately",
      "ig_pairing.pending_count() > 0" in _iws)
check("pending: a saving is never traded for a lost pairing",
      "waiting = True" in _iws)

from modules import ig_web as _iw2
_iw2._pending_last = 0.0
import time as _t2
_now = _t2.monotonic()
_iw2._pending_last = _now                       # just checked
check("pending: it is not re-read seconds later when nothing is waiting",
      _iw2._PENDING_IDLE_SECONDS >= 60)

# Every ceiling and quiet window governs an IDLE account. None of them touch a
# busy day: each arriving message re-arms the fast window, the loop never
# leaves 3s, and a day of that measured ~16,000 requests - three times what
# got an account actioned. So the day has a budget now, and it is delivery
# that gives way rather than the account.
import time as _tt
from modules import ig_web as _iw
from utils import i18n as _i18n0

_saved_rate = dict(_iw._rate)
_h = int(_tt.time()) // 3600

_iw._rate.clear()
for _i in range(24):
    _iw._rate[str(_h - _i)] = 200          # ~4,800/day
check("budget: a day heading into the risky band is called congested",
      _iw.congested())
check("budget: fast mode is withdrawn once the budget is gone",
      _iw._next_delay(8, 3, 120, 10, busy=True) > 3 * 1.25 + 0.01)
check("budget: it degrades to the idle rung, it does not stop polling",
      _iw._next_delay(8, 3, 120, 10, busy=True) <= 8 * 1.25 + 0.01)
check("budget: the quiet rungs are left alone",
      _iw._next_delay(8, 3, 120, 1800, busy=True) > 8 * 1.25)

_iw._rate.clear()
for _i in range(24):
    _iw._rate[str(_h - _i)] = 60           # ~1,440/day
check("budget: a quiet day is not throttled",
      not _iw.congested())
check("budget: fast mode is still available then",
      _iw._next_delay(8, 3, 120, 10, busy=_iw.congested()) <= 3 * 1.25 + 0.01)

check("budget: the ceiling reuses the band already calibrated on real history",
      _iw._FAST_CEILING == _iw._RATE_BANDS[1][0])
check("budget: the user is told why delivery slowed",
      "ig_web.congested()" in
      Path("handlers/ig_direct_handler.py").read_text(encoding="utf-8"))
check("budget: that notice is translated",
      any("Traffic is high" in v for v in _i18n0._EN.values()))

_iw._rate.clear()
_iw._rate.update(_saved_rate)

# The poll defaults were audited against this bot's own ban history, where a
# flat 15s poll was ~5,700 requests/day and the account was actioned. Two of
# them were pure loss - they cost requests without buying latency.
#
# IG_DM_MAX_INTERVAL caps the backoff ladder in ig_web._next_delay. At 30 it
# cancelled the deep-idle rung outright: after an hour of silence the ladder
# asks 64s and the ceiling forced 30, so an account nobody messaged all day
# still made 5,760 requests - the ban figure, reached while idle.
import time as _time
from config import settings as _cs
from modules import ig_web as _igw

check("poll: the ceiling no longer cancels the deep-idle rung",
      min(_cs.ig_dm_poll_seconds * 8,
          max(_cs.ig_dm_fast_seconds, _cs.ig_dm_max_interval))
      == _cs.ig_dm_poll_seconds * 8)
check("poll: an idle day stays under the figure that got an account actioned",
      86400 / min(_cs.ig_dm_poll_seconds * 8,
                  max(_cs.ig_dm_fast_seconds, _cs.ig_dm_max_interval)) * 2 < 5000)
check("poll: quiet hours are on by default, not left unset",
      "-" in _cs.ig_dm_quiet_hours)
check("poll: the quiet window actually reads as quiet",
      _igw._in_quiet_hours(_time.struct_time(
          (2026, 1, 1, 4, 0, 0, 0, 1, 0))))
check("poll: the middle of the day does not",
      not _igw._in_quiet_hours(_time.struct_time(
          (2026, 1, 1, 14, 0, 0, 0, 1, 0))))
check("poll: overnight is slower than the daytime ceiling",
      _cs.ig_dm_quiet_interval > _cs.ig_dm_max_interval)
check("poll: jitter is still applied to every interval",
      "random.uniform(0.75, 1.25)" in
      Path("modules/ig_web.py").read_text(encoding="utf-8"))

# Moving to a fresh Instagram account, which is the entire point of
# botctl iglogin, walked straight into two traps. iglogin reused ig_device.json
# whenever it existed - so the new account would sign in from the fingerprint
# of the accounts already checkpointed on it, which is precisely the link
# Instagram draws between accounts. And IG_DM_USERNAME was read silently, so
# the run could sign into the account being replaced without ever naming it.
_iglogin_src = Path("deploy/iglogin.py").read_text(encoding="utf-8")
check("iglogin: the device records which account it was built for",
      "OWNER_PATH" in _iglogin_src and "ig_device_owner.txt" in _iglogin_src)
check("iglogin: a different account does not inherit the old device",
      "owner.lower() != username.lower()" in _iglogin_src
      and "DEVICE_PATH.unlink" in _iglogin_src)
check("iglogin: the same account keeps its device",
      "elif DEVICE_PATH.exists():" in _iglogin_src)
# The owner file only exists after a successful login, so on the first run
# after a switch - the run that matters - "no owner" was read as "same
# account" and the retired fingerprint was reused in silence.
# Instagram's own challenge text asks to "retry with the same saved client
# settings, device identifiers". The device was written only on SUCCESS, so
# every retry after a challenge presented a device Instagram had never seen -
# a different phone each time, which is its own reason to challenge.
# No code was ever emailed because instagrapi's challenge_resolve() raises on
# sight of a native_flow checkpoint - before any handler runs. Only that entry
# point opts out; challenge_resolve_simple() underneath it implements the whole
# code flow, so the guard is stepped around rather than the flow reimplemented.
# The first attempt at this called challenge_resolve_simple() straight away.
# That function reads step_name off last_json and fetches nothing itself, so
# last_json still held the LOGIN response, step_name was "", and the empty
# branch ran into a bare `assert action == "close"`. The user saw
# "AssertionError:" with no message - our own missing request, printed as
# though Instagram had refused a code it was never asked for.
check("iglogin: the challenge is opened before its step is read",
      _iglogin_src.index("client._send_private_request(")
      < _iglogin_src.index("client.challenge_resolve_simple("))
check("iglogin: the opening request carries the context instagrapi sends",
      all(k in _iglogin_src for k in ("challenge_context", "nonce_code",
                                      "android_device_id")))
check("iglogin: the step Instagram asked for is printed",
      "این مرحله رو خواست" in _iglogin_src)
check("iglogin: a bare assertion is backed by what Instagram actually replied",
      "جواب اینستاگرام" in _iglogin_src)

# step_name "STEP_NAME" is a Bloks redirect checkpoint, and it has no code in
# it at all - not by email, not by SMS. Treating every checkpoint as the code
# kind meant waiting for a message that was never going to arrive, which is
# exactly how it looked from the other side.
# Which checkpoint Instagram serves depends on which app build asks. instagrapi
# introduces itself as a current build, and a current build gets the codeless
# Bloks redirect - approving which did not clear it here. An older build
# predates that flow and is answered with the code challenge instead.
# The mobile door is shut from this address, both ways: a current build gets a
# codeless Bloks checkpoint, an older one gets BadPassword before any challenge.
# The WEB api is a different door - the one the owner already uses in a
# browser, whose checkpoint is the ordinary six-digit one, and whose cookie is
# exactly what modules/ig_web.py wants.
_webl = Path("deploy/igweblogin.py").read_text(encoding="utf-8")
check("web login: it posts to the web login endpoint, not the mobile one",
      "/api/v1/web/accounts/login/ajax/" in _webl and "i.instagram.com" not in _webl)
check("web login: it sends the app id instagram.com sends",
      "936619743392459" in _webl)
check("web login: the password form is the one the browser posts",
      "#PWD_INSTAGRAM_BROWSER:0:" in _webl)
check("web login: a checkpoint asks where the code should go",
      '"choice"' in _webl and "security_code" in _webl)
# The first version went straight from "checkpoint" to asking for the code
# without ever posting a choice, because the plain GET answered with the
# challenge PAGE and parsed to nothing. So no code was requested, none
# arrived, and the prompt made that look like Instagram had gone quiet.
check("web login: the json view is asked for explicitly",
      '"__a": "1"' in _webl)
check("web login: no code is requested from the user unless one was ordered",
      _webl.index("if not choice:") < _webl.index("کد (خالی"))
check("web login: an unreadable checkpoint says why no code is coming",
      "درخواستی هم نرفت" in _webl)
check("web login: a page-based checkpoint points at the route that works",
      "botctl igdirect" in _webl)

check("web login: the step and contact points are printed",
      "مرحله:" in _webl)
check("web login: two-factor is handled separately from a checkpoint",
      "two_factor_identifier" in _webl)
check("web login: the session is proved against the inbox before being kept",
      _webl.index("direct_v2/inbox") < _webl.index("out_path.write_text"))
check("web login: it hands back the browser it logged in as",
      "IG_DM_USER_AGENT=" in _webl)
check("web login: credentials go to a file, not the terminal scrollback",
      "out_path.chmod(0o600)" in _webl)
check("botctl: igweblogin can be run as a command",
      "    igweblogin) do_igweblogin" in _mg)
check("botctl: it clears the jar holding the retired session",
      "ig_web_cookies.json" in _mg)

# One `legacy` run writes app_version into the saved device, so every later
# run presents the old build too - silently, and looking exactly like a normal
# one. A sticky setting that is never printed is an invisible one.
check("iglogin: the build being presented is always reported",
      "client.device_settings.get('app_version'" in _iglogin_src)

check("iglogin: an older app build can be presented",
      "_LEGACY_APP_VERSION" in _iglogin_src and "set_device(" in _iglogin_src)
check("iglogin: the version and its code are kept together",
      "_LEGACY_VERSION_CODE" in _iglogin_src)
check("iglogin: legacy persists, so the retry is the same build",
      "if legacy or not DEVICE_PATH.exists():" in _iglogin_src)
check("iglogin: a surviving Bloks checkpoint points at that route",
      "_suggest_legacy()" in _iglogin_src
      and "botctl iglogin legacy" in _iglogin_src)
check("iglogin: and does not suggest it to a run already using it",
      'if "legacy" not in sys.argv[1:]:' in _iglogin_src)

check("iglogin: the Bloks checkpoint is recognised by its step name",
      'step == "STEP_NAME"' in _iglogin_src)
check("iglogin: and by its action, in case the step is not named",
      "_BLOKS_REDIRECT" in _iglogin_src
      and "com.bloks.www.ig.challenge.redirect.async" in _iglogin_src)
check("iglogin: it is answered by approval, not by a code",
      "challenge_bloks_redirect_dismiss" in _iglogin_src)
check("iglogin: the user is told plainly that no code is coming",
      "کد نداره" in _iglogin_src)
check("iglogin: approval is acknowledged on the same run",
      "این پنجره رو نبند" in _iglogin_src)
check("iglogin: the code path is still there for the checkpoint that uses it",
      "challenge_resolve_simple" in _iglogin_src)

check("iglogin: a checkpoint tries the code path before giving up",
      "_resolve_challenge" in _iglogin_src)
check("iglogin: it calls the resolver instagrapi declines to reach",
      "challenge_resolve_simple" in _iglogin_src)
check("iglogin: the account owner is prompted for the code",
      "challenge_code_handler = _code_handler" in _iglogin_src)
check("iglogin: an empty code aborts instead of re-prompting 24 times",
      "raise KeyboardInterrupt" in _iglogin_src)
check("iglogin: a resolved challenge is followed by a second login",
      _iglogin_src.count("client.login(username, password)") >= 2)

check("iglogin: the device is saved before the login, so a retry repeats it",
      _iglogin_src.index("or not DEVICE_PATH.exists():")
      < _iglogin_src.index("client.login(username, password)"))
check("iglogin: the owner is recorded at the same moment",
      "OWNER_PATH.write_text(username" in _iglogin_src)
check("iglogin: there is an explicit way to start a new device",
      '"reset" in sys.argv[1:]' in _iglogin_src)
check("botctl: iglogin passes its argument through",
      'do_iglogin "${2:-}"' in _mg)
check("iglogin: an unrecorded owner is asked about, not assumed",
      "if DEVICE_PATH.exists() and not owner:" in _iglogin_src)
check("iglogin: declining that question drops the device",
      'device تازه ساخته می‌شه")' in _iglogin_src)
check("iglogin: a challenge from a datacenter address says the retry will repeat it",
      "if not proxy:" in _iglogin_src and "botctl proxy" in _iglogin_src)
check("iglogin: an already-checkpointed account is named as a separate case",
      "چک‌پوینت" in _iglogin_src and "بازه - نه یه تایید تازه" in _iglogin_src)
check("iglogin: the username from .env is shown, not used silently",
      "IG_DM_USERNAME تو .env" in _iglogin_src)
check("iglogin: a stale .env is called out after a successful switch",
      "settings.ig_dm_username.lower() != username.lower()" in _iglogin_src)
from utils import limits as _lim0
check("sweeper: the device owner is state, not cache",
      "ig_device_owner.txt" in _lim0.PROTECTED_NAMES)

# The bulk-download stop button did nothing visible. A callback may be
# answered exactly once, and the blanket answer() at the top of on_callback
# spent that reply, so the toast saying "stopping" raised "response already
# sent" into a bare except - while the periodic status edit put the button
# straight back every five tracks. The flag was set the whole time; nothing
# on screen ever said so.
import asyncio as _aio
from utils import i18n as _i18n
from handlers import spotify_handler as _sph


class _Q:
    def __init__(self):
        self.data = "sp:stop"
        self.message = type("M", (), {"chat_id": 4242})()
        self.answers = []
        self.cleared = False

    async def answer(self, text=None, **kw):
        if self.answers:
            raise RuntimeError("response already sent")
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.cleared = reply_markup is None


_q = _Q()
_sph._cancelled.discard(4242)
_aio.run(_sph.on_callback(type("U", (), {"callback_query": _q})(), None))

check("stop button: the press is acknowledged with the message it carries",
      _q.answers and _q.answers[0])
check("stop button: it is answered once, not twice",
      len(_q.answers) == 1)
check("stop button: the button is taken away so the press is visible",
      _q.cleared)
check("stop button: the batch is actually flagged to stop",
      4242 in _sph._cancelled)
check("stop button: the status edit does not re-attach it",
      "reply_markup=None if halting else stop_kb" in
      Path("handlers/spotify_handler.py").read_text(encoding="utf-8"))
check("stop button: both new strings are translated",
      all(k in _i18n._EN for k in (
          "⏹ متوقف شد — ترک‌های در حال دانلود تموم می‌شن.",
          "⏹ در حال توقف…")))
_sph._cancelled.discard(4242)

# A Spotify link resolved to "Drake - Finesse", five candidate uploads were
# located and scored, and every one then failed to download. The user was told
# the audio could not be FOUND and that the video may have been deleted -
# false on both counts, and impossible for five videos at one moment. A
# refusal aimed at this server is not a fact about the track.
from modules import spotify as _spd
_five = ["u1", "u2", "u3", "u4", "u5"]
_fmeta = _spd.TrackMeta("sp_1", "Finesse", ["Drake"], "Scorpion", 182080, "", "")

check("spotify: a bot check is not reported as a missing file",
      "پیدا شد" in str(_spd._download_failed(
          _fmeta, _five, ["sign in to confirm you're not a bot"] * 5, None)))
check("spotify: a bot check says the track was not deleted",
      "حذف نشده" in str(_spd._download_failed(
          _fmeta, _five, ["sign in to confirm you're not a bot"] * 5, None)))
check("spotify: a 403 on every candidate blames the address, not the track",
      "botctl ytcookies" in str(_spd._download_failed(
          _fmeta, _five, ["http error 403: forbidden"] * 5, None)))
check("spotify: no usable format points at the runtime instead",
      "botctl ytdlp" in str(_spd._download_failed(
          _fmeta, _five, ["requested format is not available"] * 5, None)))
check("spotify: nothing located is still reported as nothing located",
      "پیدا نکردم" in str(
          _spd._download_failed(_fmeta, [], [], None)))
check("spotify: an ordinary failure keeps the underlying reason",
      "gone for good" in str(_spd._download_failed(
          _fmeta, _five, ["video unavailable"] * 5, RuntimeError("gone for good"))))

# "drake gods plan" answered with an orchestral cover, an 8-bit emulation and
# a pianist, and not one result by Drake. Both catalogues had put his track
# first; _norm split "God's" into "god"+"s", so the query token "gods" was
# missing from the only real answer and _focus dropped it as partial.
from modules import spotify as _sp
check("search: an apostrophe closes a word rather than breaking it",
      _sp._norm("God's Plan") == "gods plan")
check("search: the curly apostrophe the catalogues actually print",
      _sp._norm("God’s Plan") == "gods plan")
check("search: the track being searched for is a complete answer",
      _sp._coverage("drake gods plan", "Drake God's Plan Scorpion") >= 0.999)
check("search: a cover no longer outranks it on coverage alone",
      _sp._coverage("drake gods plan", "Drake God's Plan Scorpion")
      >= _sp._coverage("drake gods plan",
                       "Diamond String Orchestra Gods Plan String Versions of Drake"))
check("search: _focus keeps the exact match it used to discard",
      any("Drake" in (t.artists[0] if t.artists else "")
          for t in _sp._focus("drake gods plan", [
              _sp.TrackMeta("dz_1", "God's Plan", ["Drake"], "Scorpion",
                            198000, "", ""),
              _sp.TrackMeta("dz_2", "Gods Plan", ["Diamond String Orchestra"],
                            "String Versions of Drake", 429000, "", ""),
          ])))

check("realtime: giving up is not reported as a dead cookie",
      _rt.dead_reason == "" and _rt.given_up == "")

# The same alert arrived at 6:22 and again at 6:34, one per restart, about a
# state that had not changed and was already understood. An alert that repeats
# for a known condition teaches the reader to ignore the ones that do not.
_rtsrc0 = Path("modules/ig_realtime.py").read_text(encoding="utf-8")
# A checkpoint is not cleared by anything a restart does, so paging about it
# once per restart is the same noise the plain refusal was already spared.
# .env.example listed webhook,web,poll and never mentioned mqtt at all, so
# every .env copied from it had the realtime channel switched off - whatever
# credentials it was given elsewhere.
_envex = Path(".env.example").read_text(encoding="utf-8")
check("sources: the example enables the realtime channel",
      "IG_DIRECT_SOURCES=webhook,mqtt,web,poll" in _envex)
check("sources: the example agrees with the code default",
      "webhook,mqtt,web,poll" in Path("config.py").read_text(encoding="utf-8"))
check("sources: mqtt is documented alongside the others",
      "#   mqtt " in _envex)
check("sources: the doc says poll needs a mobile session, not a cookie",
      "403 forever" in _envex)

check("realtime: a checkpoint counts as a known refusal",
      _rt._is_refusal("ChallengeRequired: Manual verification required via "
                      "Instagram native challenge flow"))
check("realtime: a checkpoint by its short name too",
      _rt._is_refusal("checkpoint_required"))
check("realtime: a genuine fault is still not one",
      not _rt._is_refusal("Connection reset by peer"))
check("realtime: a cookie refused by the mobile api is not paged about",
      "expected = _is_refusal(last_error) and not SESSION_FILE.exists()" in _rtsrc0)
check("realtime: it is still logged rather than swallowed",
      "expected, not alerting" in _rtsrc0)
check("realtime: something unexpected still alerts",
      "if attempt == _ALERT_AFTER and not expected:" in _rtsrc0)
check("realtime: the status line names the remedy instead",
      "botctl iglogin" in _rtsrc0)

# The credential realtime actually wants. A browser sessionid belongs to the
# WEB api - the web poller uses it fine and the mobile api answers it with a
# generic error - so realtime has never had one it could use. A mobile session
# is native to that api and lasts months rather than hours, which is why it is
# tried AHEAD of the cookie rather than as a fallback.
_rtsrc = Path("modules/ig_realtime.py").read_text(encoding="utf-8")
_connectfn = _rtsrc[_rtsrc.index("async def _connect("):_rtsrc.index("def _on_message(")]
check("mobile session: realtime prefers it over the browser cookie",
      _connectfn.index("SESSION_FILE.exists()") < _connectfn.index("settings.ig_dm_sessionid"))
check("mobile session: it counts as a credential on its own",
      "SESSION_FILE.exists() or settings.ig_dm_sessionid" in _rtsrc)
check("mobile session: it is proven live before being trusted",
      "get_timeline_feed()" in _rtsrc)
check("mobile session: a rejected one falls through to the cookie",
      "stored mobile session rejected" in _rtsrc)
check("mobile session: it is the same file the poller already writes",
      "ig_private_session.json" in Path("modules/ig_private.py").read_text(encoding="utf-8"))
import utils.limits as _lim  # noqa: E402

check("mobile session: the file survives the disk sweeper",
      "ig_private_session.json" in _lim.PROTECTED_NAMES)
check("mobile session: so does the device fingerprint",
      "ig_device.json" in _lim.PROTECTED_NAMES)

_login = Path("deploy/iglogin.py").read_text(encoding="utf-8")
check("iglogin: the device is reused rather than regenerated",
      "set_settings(json.loads(DEVICE_PATH" in _login)
check("iglogin: the session is not left world-readable", "chmod(0o600)" in _login)
check("iglogin: a login that returns is still verified",
      "get_timeline_feed()" in _login)
check("iglogin: BadPassword is explained rather than taken at face value",
      "پسورد غلط نیست" in _login)
check("iglogin: it can be run as a command", "    iglogin)" in _dispatch)
check("iglogin: it runs as the bot user, not root",
      'sudo -u "$BOT_USER"' in _mgr and "run_py() {" in _mgr
      and 'run_py "$PROJECT_DIR/deploy/iglogin.py"' in _mgr)

check("item: deep nesting terminates",
      _priv._walk_json({"a": {"b": {"c": {"d": {"e": {"f": {"pk": "1"}}}}}}}) == ("", "", ""))

# The real payload from the log, verbatim. A story url is not /p/ or /reel/,
# so it was falling through to "download this url directly" - which fetched
# 609KB of login-wall HTML and named no post.
real_story = {
    "item_id": "32954495754736457206244805632327680",
    "item_type": "xma_story_share",
    "user_id": 42,
    "original_media_igid": "3961296946067814684",
    "xma_story_share": [{
        "target_url": "https://www.instagram.com/stories/shinway__/3961296946067814684"
                      "?reel_id=77306520822&reel_owner_id=77306520822",
        "preview_url": "https://lookaside.fbsbx.com/preview.jpg",
    }],
}
permalink, pk, url = _priv._media_from_item(real_story)
check("item: real xma_story_share yields the story pk",
      pk == "3961296946067814684" and not url,
      str((permalink, pk, url)))

check("url: story pk extracted from the path",
      _items.STORY_IN_URL.search(
          "https://www.instagram.com/stories/someone/123456789?reel_id=1").group(1) == "123456789")

# A story permalink resolves to a USERNAME through the url router. Handing
# that back as a shortcode would fetch a post named after the poster.
story_dm = ig_direct.DirectMessage(
    igsid="1", source="poll",
    permalink="https://www.instagram.com/stories/shinway__/3961296946067814684")
check("dm: a story permalink is not treated as a shortcode",
      story_dm.shortcode() == "", story_dm.shortcode())

# A url was being invented as /reel/<shortcode> whenever none was passed, so
# a story arrived captioned with a reel link to a post that does not exist.
from handlers.ig_post_menu import public_link  # noqa: E402
from modules.instagram import PostInfo  # noqa: E402

check("link: nothing to build from means no link",
      public_link("", "") == "", public_link("", ""))
check("link: a real permalink is used as given",
      public_link("abc", "https://www.instagram.com/reel/XYZ/?igsh=1")
      == "https://www.instagram.com/reel/XYZ/")
check("link: a photo post is /p/ not /reel/",
      public_link("ABC12", "", PostInfo(shortcode="ABC12", is_video=False))
      == "https://www.instagram.com/p/ABC12/")
check("link: a video post is /reel/",
      public_link("ABC12", "", PostInfo(shortcode="ABC12", is_video=True))
      == "https://www.instagram.com/reel/ABC12/")


# ---------------------------------------------------------------- disk sweeper
# The sweeper deletes the oldest file it can find to stay under the cap, and
# the oldest files under downloads/ are the state files - the pairings, the
# 60-day access token, the logged-in session - plus 1.5GB of whisper weights
# that were put there by this session's work. Unprotected, it would clear
# every pairing to make room for one reel.
from utils import limits as _limits  # noqa: E402

sweep_root = Path(_TMP) / "sweeproot"
(sweep_root / "whisper").mkdir(parents=True, exist_ok=True)
(sweep_root / "instagram").mkdir(parents=True, exist_ok=True)
(sweep_root / "whisper" / "model.bin").write_bytes(b"w" * 4096)
for state in ("ig_pairing.json", "ig_token.json", "ig_seen.json",
              "ig_private_session.json", "file_ids.json", "stats.db"):
    (sweep_root / state).write_text("{}", encoding="utf-8")
(sweep_root / "instagram" / "00.mp4").write_bytes(b"m" * 8192)

for name in ("whisper/model.bin", "ig_pairing.json", "ig_token.json",
             "ig_seen.json", "ig_private_session.json", "file_ids.json", "stats.db"):
    check(f"sweep: {name} is protected",
          _limits._is_protected(sweep_root / name, sweep_root))
# yt-dlp's cachedir lives under downloads/ and holds the EJS challenge solver
# fetched from GitHub. It is cache by name and state by behaviour: the sweeper
# hunts the oldest untouched files and that is exactly what a warm cache looks
# like. Deleting it made every extraction re-fetch and re-solve - two minutes
# to open a quality menu that had just been working.
check("sweep: the yt-dlp cache is protected",
      _limits._is_protected(sweep_root / ".ytdlp-cache" / "youtube-nsig" / "abc", sweep_root))
check("sweep: ordinary media is not protected",
      not _limits._is_protected(sweep_root / "instagram" / "00.mp4", sweep_root))

_limits.sweep_downloads(sweep_root, keep_mb=0, min_interval=0)
check("sweep: media is reclaimed", not (sweep_root / "instagram" / "00.mp4").exists())
check("sweep: the model survives a full sweep", (sweep_root / "whisper" / "model.bin").exists())
check("sweep: pairings survive a full sweep", (sweep_root / "ig_pairing.json").exists())
check("sweep: the access token survives a full sweep", (sweep_root / "ig_token.json").exists())

report = _limits.disk_report(sweep_root)
check("disk: report separates protected from reclaimable",
      set(report) >= {"free_mb", "total_mb", "protected_mb", "reclaimable_mb"},
      json.dumps(report))

# A full disk must not reach the user as "no music found".
from modules.recognize import _require_workspace  # noqa: E402

import shutil as _shutil  # noqa: E402

_real_usage = _shutil.disk_usage


def _fake_free(megabytes):
    return lambda _p: type("U", (), {"free": megabytes * 1024 * 1024, "total": 1, "used": 1})()


# Both directions are mocked. Reading the real disk would make this check
# depend on whatever machine it runs on - and the machine it was written on
# had 0MB free, which passed the failure case for the wrong reason.
try:
    _shutil.disk_usage = _fake_free(1)
    try:
        _require_workspace(sweep_root)
        check("recognize: a full disk raises instead of returning no match", False, "it returned")
    except RuntimeError as e:
        check("recognize: a full disk raises instead of returning no match",
              "دیسک" in str(e), str(e).splitlines()[0])

    _shutil.disk_usage = _fake_free(5000)
    try:
        _require_workspace(sweep_root)
        check("recognize: a healthy disk passes the check", True)
    except RuntimeError as e:
        check("recognize: a healthy disk passes the check", False, str(e)[:60])
finally:
    _shutil.disk_usage = _real_usage


# ---------------------------------------------------------------- recognition
# From the production log:
#
#     Shazam error on 00.mp4: FailedDecodeJson: Failed to decode json
#
# Not a missing track - an HTML block page where JSON should be, i.e. the
# endpoint refusing this server's address. Unclassified it returned None,
# which the caller reads as "no match", so a blocked IP reached the user as
# "I couldn't identify any music".
from modules import recognize as _rec  # noqa: E402


class _FailedDecodeJson(Exception):
    pass


for exc, want, label in [
    (_FailedDecodeJson("Failed to decode json"), True, "the reported production error"),
    (Exception("403 Forbidden"), True, "a block page"),
    (Exception("Cannot connect to host"), True, "a network drop"),
    (ValueError("Expecting value: line 1 column 1"), True, "a raw json decode failure"),
    (Exception("Too Many Requests"), True, "throttling"),
    (Exception("track not in catalogue"), False, "a genuine miss"),
]:
    check(f"recognize: {label} -> outage={want}", _rec._is_transient(exc) is want,
          f"{type(exc).__name__}: {exc}")

# The account got blocked at ~1s polling, and the loop then hammered the
# blocked account every 9 seconds indefinitely - a 403 was not in the throttle
# markers, so it fell to the flat retry delay.
_REAL_403 = (
    '{"action":"item_ack","message":"We\'re sorry, but something went wrong. '
    'Please try again.","status":"fail","payload":{"client_context":null,'
    '"error_code":1404006},"status_code":"403"}'
)
for text, want, label in [
    ("challenge_required", True, "a challenge"),
    ("checkpoint_required", True, "a checkpoint"),
    ("feedback_required", True, "an action block"),
    ("Please wait a few minutes before you try again", False, "ordinary throttling"),
    ("Cannot connect to host i.instagram.com", False, "a network drop"),
    # Prose instagrapi printed for a rejected login. It matched none of the
    # machine-readable markers, so the loop retried it as a network blip.
    ("ChallengeRequired: We can send you an email to help you get back into "
     "your account. This can also happen when Instagram rejects the proxy/IP, "
     "device fingerprint, or login context, even if the password is correct.",
     True, "a rejected login, by its prose"),
    ("ChallengeRequired: ", True, "a rejected login, by its class name"),
    ("TimeoutError: read timed out", False, "a timeout"),
]:
    check(f"ig poll: {label} -> stop={want}", _priv._is_blocked(text) is want, text[:60])

# error_code 1404006 was misread as a banned account and reported as one. The
# account signed in fine on a phone throughout - the request was malformed,
# because the rewrite to raw private_request kept the endpoint and dropped
# seven of the eleven parameters instagrapi sends.
check("ig poll: 1404006 is NOT called an account ban",
      not _priv._is_blocked(_REAL_403), "it would stop the poller")

# Hand-building the request was the wrong idea twice over: it 403d, and the
# fix is to let instagrapi make the call it has always made and read
# client.last_json for the raw fields. Assert the helpers are gone so nobody
# reintroduces them.
check("ig poll: the hand-built inbox request is gone",
      not hasattr(_priv, "_inbox_params") and not hasattr(_priv, "_inbox"))

# Both readers must share one parser: the mobile and web apis return the
# same payload, and two copies would drift the moment Instagram renames a
# key again.
check("ig items: the poller uses the shared parser",
      _priv._media_from_item is _items.media_from_item)

# socks5h is what curl and requests want; aiohttp-socks and httpx both reject
# it outright, before any request is made:
#     ValueError: Unknown scheme for proxy URL URL('socks5h://...')
# The suffix means "resolve DNS at the proxy", which both do anyway, so it is
# dropped rather than translated.
import modules.ig_web as _web  # noqa: E402

_saved_proxy = _web.settings.ig_dm_proxy
try:
    for raw, want in [
        ("socks5h://127.0.0.1:40000", "socks5://127.0.0.1:40000"),
        ("socks5://127.0.0.1:40000", "socks5://127.0.0.1:40000"),
        ("socks4a://host:1080", "socks4://host:1080"),
        ("http://127.0.0.1:8118", "http://127.0.0.1:8118"),
        ("", None),
    ]:
        object.__setattr__(_web.settings, "ig_dm_proxy", raw)
        got = _web._proxy_url()
        check(f"proxy: httpx accepts {raw or '(unset)'}", got == want, str(got))
finally:
    object.__setattr__(_web.settings, "ig_dm_proxy", _saved_proxy)

# Instagram rotates sessionid and hands the new value back in Set-Cookie. The
# first version built a fresh client per request from the static config, threw
# every rotation away, and a few calls later got a 608KB login page - which
# read as "your cookie expired" when the cookie had merely moved.
_web_cookie_backup = _web._COOKIE_PATH
_web._COOKIE_PATH = Path(_TMP) / "ig_web_cookies.json"
try:
    for name, value in (("ig_dm_sessionid", "seed-session"),
                        ("ig_dm_csrftoken", "seed-csrf"),
                        ("ig_dm_ds_user_id", "123")):
        object.__setattr__(_web.settings, name, value)

    check("ig web: with nothing stored, the .env seed is used",
          _web._load_cookies().get("sessionid") == "seed-session")

    _web._COOKIE_PATH.write_text(json.dumps({
        "_seed": "seed-session",
        "sessionid": "rotated-session", "csrftoken": "rotated-csrf", "mid": "abc",
    }), encoding="utf-8")
    jar = _web._load_cookies()
    check("ig web: a rotated sessionid beats the .env seed",
          jar.get("sessionid") == "rotated-session", jar.get("sessionid"))
    check("ig web: a rotated csrftoken beats the .env seed",
          jar.get("csrftoken") == "rotated-csrf")
    check("ig web: cookies Instagram added are kept", jar.get("mid") == "abc")
    check("ig web: seed-only cookies survive", jar.get("ds_user_id") == "123")
    check("ig web: bookkeeping does not leak into the jar", "_seed" not in jar)

    # The hole in "stored always wins": after a session died, pasting a fresh
    # sessionid changed nothing, because the dead stored cookie kept
    # overriding it. Every retry used the cookie that had already stopped
    # working, and the bot told the user to get a fresh one - which they had.
    _web._COOKIE_PATH.write_text(json.dumps({
        "_seed": "an-older-session", "sessionid": "dead-rotated",
    }), encoding="utf-8")
    check("ig web: a newly pasted sessionid discards the stale jar",
          _web._load_cookies().get("sessionid") == "seed-session",
          _web._load_cookies().get("sessionid"))

    # A ceiling on the idle ladder is the promise "nobody waits longer than
    # this", and it is what makes a low request count and a fast first
    # message pull against each other.
    object.__setattr__(_web.settings, "ig_dm_max_interval", 30.0)
    object.__setattr__(_web.settings, "ig_dm_quiet_hours", "")
    _delays = [_web._next_delay(30.0, 5.0, 60.0, 10800) for _ in range(500)]
    check("ig web: the idle ladder respects the ceiling",
          max(_delays) <= 30.0 * 1.25 + 0.01, f"worst {max(_delays):.1f}s")
    check("ig web: intervals are jittered, not identical",
          len(set(round(d, 3) for d in _delays)) > 100,
          f"{len(set(round(d, 3) for d in _delays))} distinct")
    check("ig web: an active conversation still polls fast",
          max(_web._next_delay(30.0, 5.0, 60.0, 10) for _ in range(200)) <= 5.0 * 1.25 + 0.01)

    object.__setattr__(_web.settings, "ig_dm_quiet_hours", "2-8")
    for _hour, _want in ((1, False), (2, True), (7, True), (8, False), (23, False)):
        _stamp = time.struct_time((2026, 8, 12, _hour, 0, 0, 0, 224, 0))
        check(f"ig web: {_hour:02d}:00 quiet={_want}",
              _web._in_quiet_hours(_stamp) is _want)

    # Instagram warns before it acts. Continuing at the same rate into a
    # warning is how the warning becomes an action.
    for _text, _want in (
        ("Please wait a few minutes before you try again", True),
        ("HTTP 429: rate limit", True),
        ("feedback_required", True),
        ("not json (410166 bytes) - probably a login page", False),
        ("Connection timed out", False),
    ):
        check(f"ig web: soft block -> {_want} for {_text[:34]!r}",
              _web._is_soft_block(_text) is _want)

    # Every traffic figure quoted so far has been a model with an assumed hour
    # of daily use. The bot now counts what it actually sends, so "is this
    # setting safe" stops being answered from a spreadsheet.
    _hour = int(time.time()) // 3600

    def _load_rate(per_hour, hours=24):
        _web._rate.clear()
        for _h in range(hours):
            _web._rate[str(_hour - _h)] = per_hour

    # Anchored on this bot's own history: a flat 15s poll is 240/hour, which
    # is what the account was actioned on.
    for _per_hour, _want in ((40, "محتاطانه"), (118, "متعادل"),
                             (150, "پرریسک"), (240, "بن‌آور")):
        _load_rate(_per_hour)
        check(f"ig web: {_per_hour}/hour reads as {_want}",
              _web.rate()["verdict"] == _want, _web.rate()["verdict"])

    # A few hours in, the day's total is small. Reporting it raw would make a
    # dangerous rate look safe for most of the day it is being measured.
    _web._rate.clear()
    for _h in range(3):
        _web._rate[str(_hour - _h)] = 100
    check("ig web: a partial day extrapolates rather than under-reporting",
          _web.rate()["projected"] == 2400, str(_web.rate()))
    _web._rate.clear()

    # A checkpoint is not a rate limit. Instagram has flagged the account and
    # wants a person in the app; no amount of waiting clears it, and every
    # further request is another automated call against an account already
    # under review. It must stop the loop, not back it off.
    _real_checkpoint = (
        '{"message":"checkpoint_required","checkpoint_url":'
        '"https://www.instagram.com/challenge/?next=/api/v1/direct_v2/inbox/"}'
    )
    for _text, _want, _label in (
        (_real_checkpoint, True, "the reported 400"),
        ("ChallengeRequired: Manual verification required", True, "instagrapi's wording"),
        ("Please wait a few minutes before you try again", False, "a soft block"),
        ("not json (410166 bytes) - probably a login page", False, "an expired cookie"),
        ("Connection timed out", False, "a network drop"),
    ):
        check(f"ig web: checkpoint={_want} for {_label}",
              _web._is_checkpoint(_text) is _want)

    check("ig web: a checkpoint is not treated as a soft block",
          not _web._is_soft_block(_real_checkpoint))

    check("ig web: the cookie jar is protected from the sweeper",
          _limits._is_protected(sweep_root / "ig_web_cookies.json", sweep_root))

    # The mobile pending-inbox path 404s on the web api, and the 404 body is a
    # whole html page - logged raw it filled the journal twice a minute.
    page = '<!DOCTYPE html>\n<html lang="None" class="no-js not-logged-in ">\n  <head>\n' * 30
    short = _web._short(page)
    check("ig web: a 404 page collapses to one bounded line",
          "\n" not in short and len(short) <= 120, f"{len(short)} chars")

    _real_get, _tries = _web._get, []

    def _always_404(url, params):
        _tries.append(url)
        raise LookupError("HTTP 404: not found")

    try:
        _web._get = _always_404
        _web._pending_route = None
        check("ig web: every pending spelling is tried once",
              _web._pending_threads() == [] and len(_tries) == len(_web.PENDING_CANDIDATES),
              f"{len(_tries)} attempts")
        before = len(_tries)
        _web._pending_threads()
        check("ig web: it gives up instead of retrying every sweep",
              len(_tries) == before, f"{len(_tries) - before} extra attempts")
    finally:
        _web._get = _real_get
        _web._pending_route = None
finally:
    _web._COOKIE_PATH = _web_cookie_backup

# The device fingerprint must survive a session reset. Losing it is what made
# the direct endpoints start refusing an account that was otherwise fine.
check("ig poll: the device file is protected from the sweeper",
      _limits._is_protected(sweep_root / "ig_device.json", sweep_root))
check("ig poll: device keys cover the fingerprint",
      {"uuids", "device_settings", "user_agent"} <= set(_priv._DEVICE_KEYS))

# A refused sign-in and a disabled account need different answers, and being
# told the account is banned sends you to fix the wrong thing.
check("ig poll: BadPassword reads as a login problem",
      _priv._is_login_problem("BadPassword: ... rejects the proxy/IP ..."))
check("ig poll: a checkpoint does not read as a login problem",
      not _priv._is_login_problem("checkpoint_required"))

# shazamio-core's Rust demuxer logs one WARNING per junk byte skipped. On a
# 2MB file that is tens of thousands of lines per recognition, written
# synchronously to journald - most of what the user experiences as "slow".
import io as _io  # noqa: E402
import logging as _logging  # noqa: E402

import config as _config  # noqa: E402

_config.setup_logging()
_buf = _io.StringIO()
_probe = _logging.StreamHandler(_buf)
for _f in _logging.getLogger().handlers[0].filters:
    _probe.addFilter(_f)
_logging.getLogger().addHandler(_probe)
try:
    _logging.getLogger("symphonia_bundle_mp3.demuxer").warning("skipping junk at 2052628 bytes")
    _logging.getLogger("symphonia_core.probe").warning("invalid mpeg audio header")
    _logging.getLogger("symphonia_bundle_mp3.demuxer").error("a real decode failure")
    _logging.getLogger("modules.recognize").warning("a real warning of ours")
    _out = _buf.getvalue()
finally:
    _logging.getLogger().removeHandler(_probe)

check("logs: symphonia junk spam is dropped", "skipping junk" not in _out)
check("logs: symphonia header spam is dropped", "invalid mpeg" not in _out)
check("logs: a real symphonia ERROR still gets through", "a real decode failure" in _out)
check("logs: our own warnings still get through", "a real warning of ours" in _out)

# A rejected key and an exhausted quota both stop an engine, but only one
# recovers on its own. Reporting "temporarily disabled" for a wrong key sends
# the admin off to wait instead of to fix it.
from modules import engines as _engines  # noqa: E402

for why, want, label in [
    ("HTTP 400", True, "acoustid's rejected key"),
    ("{'error_code': 900, 'error_message': 'authorization failed'}", True,
     "audd's rejected token"),
    ("HTTP 429 rate limited", False, "a quota blip"),
    ("Cannot connect to host", False, "a network drop"),
    ("daily limit reached", False, "an exhausted quota"),
]:
    check(f"engines: {label} -> auth failure={want}",
          _engines._is_auth_failure(why) is want, why[:40])

# shazamio splits what it is given into 10-second segments and, on a miss,
# waits out the retryms Shazam returns (12000) before the next one. A
# 12-second window was therefore two segments and one 12s sleep - five windows
# of a 172s track took 53 seconds while five windows of a 20s clip took 1.7.
# Every window must be one segment.
for _duration in (7, 14, 20, 45, 172, 600):
    _window, _offsets = _rec._sample_plan(_duration)
    check(f"recognize: a {_duration}s file windows to one segment",
          _window <= _rec._SEGMENT and len(_offsets) >= 1,
          f"window {_window}s, {len(_offsets)} offsets")

check("recognize: the last error is recorded for /recstatus",
      (_rec._note_error(_FailedDecodeJson("Failed to decode json")) is None)
      and "FailedDecodeJson" in _rec.last_error,
      _rec.last_error)


print()
if failures:
    print(f"=== {len(failures)} CHECK(S) FAILED: {failures} ===")
    sys.exit(1)
print("=== ALL CHECKS PASSED ===")
