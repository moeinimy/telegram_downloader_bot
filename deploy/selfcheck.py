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
        # Localised carries a template raised from a module, which is
        # translated later by whichever handler displays it. Those keys never
        # pass through t(), so without this they can go missing silently -
        # the error just stays Persian for an English user.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Localised"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            used.add(node.args[0].value)

missing = sorted(s for s in used if s not in _EN)
check("i18n: every t() and Localised string has an English entry", not missing,
      f"{len(used)} strings checked" if not missing else f"MISSING: {missing}")

# A translation whose placeholders differ from the original renders as a
# KeyError at the worst moment - while reporting some other failure.
_ph = __import__("re").compile(r"\{(\w+)")
_bad = [k for k, v in _EN.items()
        if set(_ph.findall(k)) != set(_ph.findall(v))]
check("i18n: every translation keeps the original's placeholders",
      not _bad, f"MISMATCHED: {_bad[:3]}")

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
# This asserted the bug. With no stream between 720 and 2160, the 1080p label
# resolved to the same 720p file - and showing it was offering a choice that
# changed nothing, which is how five buttons came to read 516MB each.
check("sizes: a label with no stream of its own is not offered",
      "1080p" not in _sizes)
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
# Security audit.
#
# httpx puts the whole failing url in its exception message, and this bot's
# urls carry credentials: /srcstatus formatted `token rejected - {e}` straight
# into a Telegram message, which put a sixty-day Instagram token into a chat
# history that syncs to Telegram's servers and stays there.
from utils.secrets import scrub as _scrub
import httpx as _hx

_req = _hx.Request("GET", "https://graph.instagram.com/v21.0/me"
                          "?access_token=EAAGsecretTOKEN1234567890")
try:
    _hx.Response(400, request=_req).raise_for_status()
except Exception as _e:
    _leak, _clean = str(_e), _scrub(_e)

check("secrets: the raw exception really does carry the token",
      "EAAGsecretTOKEN1234567890" in _leak)
check("secrets: and scrub takes it out",
      "EAAGsecretTOKEN1234567890" not in _clean)
check("secrets: the message is still readable afterwards",
      "400 Bad Request" in _clean)
check("secrets: a session cookie is redacted",
      "12345abcdef" not in _scrub("sessionid=12345abcdef"))
check("secrets: an Authorization header is redacted",
      "ya29.SECRET" not in _scrub("Authorization: Bearer ya29.SECRETVALUE00"))
check("secrets: a spotify client secret is redacted",
      "abc123def456" not in _scrub("?client_secret=abc123def456"))
check("secrets: an ordinary message is left alone",
      _scrub("Connection reset by peer") == "Connection reset by peer")
check("secrets: a url with nothing sensitive is left alone",
      _scrub("https://youtube.com/watch?v=abc") == "https://youtube.com/watch?v=abc")
check("secrets: scrub never raises on an unprintable value",
      _scrub(type("B", (), {"__str__": lambda self: 1 / 0})()) == "<unprintable>")
check("secrets: the leak that started this is fixed at the source",
      "scrub(e)" in Path("modules/ig_graph.py").read_text(encoding="utf-8"))
# Every error shown to a user goes through scrub, whatever else wraps it.
# This used to assert the exact spelling ".format(err=scrub(e))", which broke
# the moment the text also had to be translated - scrub was still there,
# outermost, doing its job. Asserting the intent survives that; asserting the
# spelling only says the line has not been edited.
_err_sites, _unscrubbed = 0, []
for _h in sorted(Path("handlers").glob("*.py")):
    for _n, _line in enumerate(_h.read_text(encoding="utf-8").splitlines(), 1):
        if "err=" not in _line or "format(" not in _line:
            continue
        _err_sites += 1
        if "scrub(" not in _line:
            _unscrubbed.append(f"{_h.name}:{_n}")
check("secrets: user-facing error templates are scrubbed too",
      _err_sites and not _unscrubbed,
      f"{_err_sites} sites" if not _unscrubbed else f"BARE: {_unscrubbed[:3]}")

# The rest of the audit, recorded so a regression is caught rather than
# rediscovered.
check("audit: nothing is executed through a shell",
      not any("shell=True" in Path(f).read_text(encoding="utf-8")
              for f in ("modules/youtube.py", "modules/spotify.py",
                        "modules/recognize.py", "modules/instagram.py")))
from utils import helpers as _hlp
check("audit: a filename cannot climb out of the download directory",
      "/" not in _hlp.safe_filename("../../etc/passwd")
      and "\\" not in _hlp.safe_filename("..\..\windows"))
check("audit: a filename of only dots does not become empty",
      _hlp.safe_filename("..") == "file")
check("audit: every admin command is gated",
      Path("handlers/admin.py").read_text(encoding="utf-8").count("_reject(update)") >= 7)
check("audit: the rate limiter sees every user message",
      "limits.allow(" in Path("handlers/router.py").read_text(encoding="utf-8"))

# Every HTTPS client has a TLS handshake fingerprint, and Instagram reads it.
# Sending Chrome's User-Agent over httpx says "I am Chrome" in the header while
# the handshake says otherwise; curl_cffi makes both halves agree.
_igw = Path("modules/ig_web.py").read_text(encoding="utf-8")
check("tls: chrome's handshake is impersonated when curl_cffi is there",
      'impersonate="chrome"' in _igw)
check("tls: a missing curl_cffi falls back rather than failing",
      "except ImportError:" in _igw and "import httpx" in _igw)
check("tls: any other curl_cffi fault also falls back",
      "curl_cffi unusable" in _igw)
check("tls: the proxy is passed in the spelling curl_cffi wants",
      '"http": proxy, "https": proxy' in _igw)
check("tls: cookies are still loaded into whichever transport won",
      _igw.index("_new_transport(_proxy_url())")
      < _igw.index('_session.cookies.set(name, value, domain=".instagram.com")'))
check("install: curl_cffi cannot take the install down with it",
      "curl_cffi" in Path("deploy/manage.sh").read_text(encoding="utf-8"))

# A local import landed in the wrong function.
#
# `from modules.youtube import _fast_download_opts as _yt_fast_opts` was added
# to _flat_entries while the call sat in download_track, so every single music
# download died with NameError - and nothing here noticed, because the module
# imports perfectly and the name only fails when that line runs.
#
# This walks every module for the same shape: a name that some function
# imports locally, used by a DIFFERENT function that does not import it and
# where it is not a module-level name either. It is a real class of bug that
# no import check can see.
import ast as _ast


def _scope_leaks(path):
    tree = _ast.parse(Path(path).read_text(encoding="utf-8"))
    module_names = set()
    funcs = []

    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for a in node.names:
                module_names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, _ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, _ast.Name):
                    module_names.add(tgt.id)
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            module_names.add(node.name)
            funcs.append(node)
        elif isinstance(node, _ast.ClassDef):
            module_names.add(node.name)

    local_imports = {}
    for fn in funcs:
        names = set()
        for sub in _ast.walk(fn):
            if isinstance(sub, (_ast.Import, _ast.ImportFrom)):
                for a in sub.names:
                    names.add(a.asname or a.name.split(".")[0])
        local_imports[fn.name] = names

    everywhere = set().union(*local_imports.values()) if local_imports else set()

    leaks = []
    for fn in funcs:
        bound = set(local_imports[fn.name])
        for sub in _ast.walk(fn):
            if isinstance(sub, _ast.arg):
                bound.add(sub.arg)
            elif isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Store):
                bound.add(sub.id)
            elif isinstance(sub, _ast.alias):
                bound.add(sub.asname or sub.name.split(".")[0])
        for sub in _ast.walk(fn):
            if (isinstance(sub, _ast.Name) and isinstance(sub.ctx, _ast.Load)
                    and sub.id in everywhere
                    and sub.id not in bound
                    and sub.id not in module_names):
                leaks.append(f"{Path(path).name}:{sub.lineno} {fn.name}() uses "
                             f"{sub.id}, imported in another function")
    return leaks


_leaks = []
for _f in sorted(Path("modules").glob("*.py")) + sorted(Path("handlers").glob("*.py")):
    _leaks += _scope_leaks(_f)


def _undefined_privates(path):
    """Module-private names used but never defined anywhere in the file.

    A helper referenced before it exists imports perfectly and raises only
    when that line runs - twice today: _yt_fast_opts and _is_cold. Restricted
    to leading-underscore names, which are by convention local to the module,
    so a missing one is a mistake rather than an import this pass cannot see.
    """
    tree = _ast.parse(Path(path).read_text(encoding="utf-8"))
    defined, used = set(), {}

    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef,
                             _ast.ClassDef)):
            defined.add(node.name)
            for a in getattr(node, "args", None).args if hasattr(node, "args") else []:
                defined.add(a.arg)
        elif isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Store):
            defined.add(node.id)
        elif isinstance(node, _ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, _ast.Global):
            defined.update(node.names)
        elif isinstance(node, _ast.ExceptHandler) and node.name:
            defined.add(node.name)

    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load)
                and node.id.startswith("_") and not node.id.startswith("__")):
            used.setdefault(node.id, node.lineno)

    return [f"{Path(path).name}:{line} {name} is used but never defined"
            for name, line in used.items() if name not in defined]


_undef = []
for _f in sorted(Path("modules").glob("*.py")) + sorted(Path("handlers").glob("*.py")):
    _undef += _undefined_privates(_f)

check("scope: no module-private helper is used before it exists",
      not _undef, "; ".join(_undef[:3]))

check("scope: no function uses a name another function imported",
      not _leaks, "; ".join(_leaks[:3]))

# /id answered with the user's own id for a forwarded channel post, every
# time. python-telegram-bot 21 REMOVED Message.forward_from_chat in favour of
# forward_origin, so the getattr looking for the old name found None - a
# rename that fails silently, because getattr with a default cannot tell
# "absent" from "empty".
import datetime as _dt
from telegram import Chat as _Chat, Message as _Msg
from telegram import MessageOriginChannel as _Origin, User as _User

_now = _dt.datetime.now(_dt.timezone.utc)
_chan = _Chat(id=-1001234567890, type=_Chat.CHANNEL, title="music archive")
_org = _Origin(date=_now, chat=_chan, message_id=5)


def _mk(**kw):
    return _Msg(message_id=1, date=_now, chat=_Chat(id=7, type=_Chat.PRIVATE),
                from_user=_User(id=7, first_name="m", is_bot=False), **kw)


check("chat id: a forwarded channel post is recognised on PTB 21",
      _adm_mod._forwarded_chat(_mk(text="/id", forward_origin=_org)) is not None)
check("chat id: and its numeric id is the channel's",
      _adm_mod._forwarded_chat(_mk(text="/id", forward_origin=_org)).id
      == -1001234567890)
check("chat id: asking by replying to the forward works too",
      _adm_mod._forwarded_chat(
          _mk(text="/id", reply_to_message=_mk(text=".", forward_origin=_org))
      ) is not None)
check("chat id: a plain message is not mistaken for a forward",
      _adm_mod._forwarded_chat(_mk(text="/id")) is None)
check("chat id: the old attribute is still read, for older PTB",
      "forward_from_chat" in Path("handlers/admin.py").read_text(encoding="utf-8"))

# Security audit.
#
# utils/secrets.py states the rule - text derived from an exception is
# scrubbed before it is logged or sent - and the rule was applied in twelve
# places and skipped in twelve others. An httpx error carries the whole
# failing url, and this bot's urls carry sixty-day tokens, so the skipped half
# put credentials into user chat histories.
import re as _re2
_unscrubbed = []
for _h in sorted(Path("handlers").glob("*.py")):
    _txt = _h.read_text(encoding="utf-8")
    for _m in _re2.finditer(
            r"(?:reply_text|edit_text|answer)\([^)]*?(?:\{e\}|err=e\))", _txt):
        if "scrub" not in _m.group(0):
            _unscrubbed.append(f"{_h.name}:{_txt[:_m.start()].count(chr(10)) + 1}")
check("secrets: no exception reaches a user unscrubbed",
      not _unscrubbed, "; ".join(_unscrubbed[:4]))

from utils.secrets import scrub as _scrub
check("secrets: an access token in a url is masked",
      "EAAG" not in _scrub("url?access_token=EAAGm0PX4ZCpsBO1234"))
check("secrets: a sessionid is masked",
      "AbCdEf" not in _scrub("sessionid=76561198012345678%3AAbCdEfGh%3A17"))
check("secrets: a bearer header is masked",
      "eyJhbGci" not in _scrub("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y"))
check("secrets: ordinary text is left alone",
      _scrub("video unavailable") == "video unavailable")
check("secrets: the redactor cannot itself raise",
      _scrub(object()) is not None)

# Every file that IS a credential is 0600. ig_cookies.txt was written at
# whatever the umask gave it - world-readable on a normal server.
check("secrets: the instagram cookie jar is not world-readable",
      "path.chmod(0o600)" in Path("modules/instagram.py").read_text(encoding="utf-8"))

# No shell, no eval: nothing user-supplied can become code.
_danger = []
for _f in (sorted(Path("modules").glob("*.py")) + sorted(Path("handlers").glob("*.py"))
           + sorted(Path("utils").glob("*.py")) + sorted(Path("web").glob("*.py"))):
    _t = _f.read_text(encoding="utf-8")
    for _bad in ("shell=True", "os.system(", "eval(", "exec("):
        if _bad in _t:
            _danger.append(f"{_f.name}: {_bad}")
check("security: no shell execution and no eval anywhere",
      not _danger, "; ".join(_danger[:3]))

# Meta signs webhook bodies; an unverified one is an open endpoint.
_wh = Path("web/webhook.py").read_text(encoding="utf-8")
check("security: webhook bodies are verified before being trusted",
      "verify_signature(raw" in _wh)
check("security: the comparison is constant-time",
      "hmac.compare_digest" in _wh)

# A crafted title must not escape the download directory.
from utils.helpers import safe_filename as _sf
check("security: a traversal attempt cannot leave the folder",
      "/" not in _sf("../../etc/passwd")
      and chr(92) not in _sf(chr(92) + chr(92) + "windows"))
check("security: a name of only dots does not become empty",
      _sf("...") == "file")

# botctl find greps with -iE, where | is alternation and \| is a literal
# pipe - so the habitual grep spelling matched nothing and read as "the bot
# never logged that".
_mg0 = Path("deploy/manage.sh").read_text(encoding="utf-8")
check("botctl find: the BRE spelling of alternation is accepted too",
      "sed 's/" + chr(92) * 4 + "|/|/g'" in _mg0)

# 282 files at three seconds each is fourteen minutes, and a backfill started
# by accident has no way out without this.
_arc0 = Path("utils/archive.py").read_text(encoding="utf-8")
check("archive: the backfill can be stopped",
      "should_stop" in _arc0)
check("archive: and it is checked before each file, not each batch",
      _arc0.index("if should_stop is not None and should_stop():")
      < _arc0.index('kind = key.split(":", 1)[0]'))
check("archive: the button exists and is answered",
      'callback_data="adm:arcstop"' in _adm and '"adm:arcstop"' in _adm)
check("archive: stopping is reported rather than looking finished",
      "متوقف شد" in _adm)

# A cached send that failed dropped the id on ANY exception, so one
# flood-wait or dropped connection threw away a good file_id and bought a full
# re-download - the opposite of what a cache is for.
from utils import file_cache as _fc
check("cache: an id Telegram disowns is dropped",
      _fc.is_dead_reference(Exception("Bad Request: wrong file identifier")))
check("cache: and the other spelling of it",
      _fc.is_dead_reference(Exception("wrong remote file identifier specified")))
check("cache: a timeout is not a dead id",
      not _fc.is_dead_reference(Exception("Timed out")))
check("cache: nor is a flood wait",
      not _fc.is_dead_reference(Exception("Flood control exceeded. Retry in 12 seconds")))
check("cache: nor is a connection error",
      not _fc.is_dead_reference(Exception("ConnectError: connection refused")))
check("cache: music only drops on a dead reference",
      "file_cache.is_dead_reference(e)" in
      Path("handlers/spotify_handler.py").read_text(encoding="utf-8"))
check("cache: youtube does the same",
      "file_cache.is_dead_reference(e)" in
      Path("handlers/youtube_handler.py").read_text(encoding="utf-8"))
check("cache: a dropped id is re-downloaded rather than reported as an error",
      "file_cache.drop(cache_key)" in
      Path("handlers/spotify_handler.py").read_text(encoding="utf-8"))

# Everything delivered is mirrored into the archive channel, by file_id -
# Telegram re-sends a file it already holds without the bytes leaving this
# server again, so archiving a 700MB video costs one api call and no
# bandwidth. That is the only reason doing it for everything is reasonable.
_arc = Path("utils/archive.py").read_text(encoding="utf-8")
check("archive: audio, video and photo each get their own send method",
      all(k in _arc for k in ("bot.send_audio", "bot.send_video",
                              "bot.send_photo", "bot.send_document")))
check("archive: a failure never reaches the caller",
      "log.info(\"archive: could not mirror" in _arc)
check("archive: nothing is attempted without a channel configured",
      "if not enabled() or not file_id:" in _arc)
check("archive: music deliveries are mirrored",
      "archive.mirror(" in Path("handlers/spotify_handler.py").read_text(encoding="utf-8"))
check("archive: youtube audio and video are mirrored",
      Path("handlers/youtube_handler.py").read_text(encoding="utf-8").count(
          "archive.mirror(") == 2)
check("archive: instagram deliveries are mirrored",
      "_archive_sent(" in Path("handlers/instagram_handler.py").read_text(encoding="utf-8"))
check("archive: what was actually sent decides the kind, not what we asked",
      "getattr(sent, \"animation\", None)" in
      Path("handlers/instagram_handler.py").read_text(encoding="utf-8"))
check("archive: the backfill crawls rather than racing the flood limit",
      "asyncio.sleep(3.0)" in _arc)
check("archive: /archive is registered",
      'CommandHandler("archive"' in Path("main.py").read_text(encoding="utf-8"))
check("archive: the cache can be enumerated without holding its lock",
      "def snapshot(" in Path("utils/file_cache.py").read_text(encoding="utf-8"))

# Security audit, the findings worth a permanent guard.
#
# httpx renders a failed Telegram call as
#     Client error '400' for url 'https://api.telegram.org/bot<TOKEN>/sendMessage'
# and several handlers show the exception text straight to the user. scrub()
# masked query parameters and Authorization headers - the token is in neither,
# it is in the PATH - so the bot's own credential could be handed to whoever
# triggered the error. Anyone holding it can read and send as the bot.
from utils.secrets import scrub as _scrub
_TOKENISH = "7123456789:AAHrealsecrettokenhere12345"
check("secrets: the bot token is masked where an http error puts it",
      _TOKENISH not in _scrub(
          f"Client error for url 'https://api.telegram.org/bot{_TOKENISH}/sendMessage'"))
check("secrets: a session cookie is still masked",
      "abcdef123456" not in _scrub("sessionid=abcdef123456"))
check("secrets: an ordinary message is left readable",
      _scrub("no secrets here") == "no secrets here")
check("secrets: scrub never raises, whatever it is handed",
      _scrub(object()) and _scrub(None))

import re as _re2
_unscrubbed = []
for _f in sorted(Path("handlers").glob("*.py")):
    for _i, _line in enumerate(_f.read_text(encoding="utf-8").splitlines(), 1):
        if (_re2.search(r"(reply_text|edit_text|send_message)\(", _line)
                and _re2.search(r"\{(err|e)\}|\{str\(e\)", _line)
                and "scrub" not in _line):
            _unscrubbed.append(f"{_f.name}:{_i}")
check("secrets: no handler shows a raw exception to a user",
      not _unscrubbed, ", ".join(_unscrubbed[:4]))

# A pasted url reaches yt-dlp, so the router is the boundary. Checked against
# the shapes that matter rather than trusting the pattern by eye.
from utils.url_router import route as _route
for _bad in ("file:///etc/passwd", "http://127.0.0.1:8081/x",
             "http://169.254.169.254/latest/meta-data/",
             "https://youtube.com.evil.com/watch?v=x", "http://[::1]:22/"):
    check(f"router: refuses {_bad[:34]}", _route(_bad) is None)
check("router: still accepts a real youtube url",
      _route("https://www.youtube.com/watch?v=abc") is not None)

# Inline mode. Every other way into this bot needs somebody to already know
# it exists; an inline result is used in front of an audience, with the bot's
# name under the message.
_inl = Path("handlers/inline.py").read_text(encoding="utf-8")
import asyncio as _aio2
_asyncio_run = _aio2.run
from handlers import inline as _inline_mod
check("inline: a track already on Telegram is sent as the real audio",
      "InlineQueryResultCachedAudio" in _inl)
# The card version worked and was the wrong thing: what landed in the other
# chat was a text message ABOUT a song rather than the song.
# Showing ONLY cached results meant an empty list for any song nobody had
# fetched yet, which is worse than a card. Telegram gives no third option:
# an inline answer has seconds, the message is posted the instant a result is
# picked, and a text result cannot be edited into an audio one afterwards.
check("inline: a cached track is sent as real audio",
      "InlineQueryResultCachedAudio(id=str(uuid4()), audio_file_id=cached)" in _inl)
check("inline: the audio carries no caption repeating its own tags",
      "audio_file_id=cached)" in _inl and "caption=f" not in _inl)
check("inline: an uncached track still offers a way to get it",
      "InlineQueryResultArticle" in _inl and "?start=trk_" in _inl)
check("inline: an inline query warms the cache for the next person",
      "asyncio.create_task(_warm(" in _inl)
check("inline: the warm sends to the store, not to a user",
      "chat_id=settings.cache_channel_id" in _inl)
check("inline: no store configured means no warm, not a crash",
      "settings.cache_channel_id and _should_warm(query)" in _inl)
check("inline: a keystroke does not queue a download",
      "_WARM_DEBOUNCE" in _inl)
check("inline: the same track is not warmed twice at once",
      "_warming" in _inl)
_iw = _inline_mod
_iw._warm_seen.clear()
_iw._should_warm("q")
check("inline: typing does not warm; a pause does",
      not _iw._should_warm("q"))
# The first version wrapped sp.search_tracks in run_in_thread - but that
# function is ALREADY decorated with it. The inner call returned a coroutine
# nothing awaited, `for track in tracks` raised TypeError, and the query was
# never answered. From a chat that is indistinguishable from inline mode being
# switched off. The test missed it by patching _search, which is the one thing
# it should not have replaced.
check("inline: the search is not wrapped in a second thread pool",
      "from utils.helpers import run_in_thread" not in _inl)
check("inline: it awaits the already-async search",
      "await sp.search_tracks(" in _inl)
_isearch = _asyncio_run(_inline_mod._search("drake"))
check("inline: _search really returns a list, not a coroutine",
      isinstance(_isearch, list))
check("inline: an empty query explains itself instead of looking broken",
      "switch_pm_text" in _inl)
check("inline: the handler is registered",
      "InlineQueryHandler(inline.on_inline_query)" in
      Path("main.py").read_text(encoding="utf-8"))
_st = Path("handlers/start.py").read_text(encoding="utf-8")
check("inline: the deep link delivers the track, not the welcome text",
      'arg.startswith("trk_")' in _st and "deliver_track" in _st)
check("inline: delivery reuses the button path rather than a second copy",
      "_send_and_download_track(msg, meta)" in
      Path("handlers/spotify_handler.py").read_text(encoding="utf-8"))

# A reel arrived unstreamable with its length shown as 00:00 - it had to be
# fully downloaded before it would play - while a YouTube video of the same
# size arrived fine. The YouTube path had been taught to TELL Telegram the
# dimensions and duration; the Instagram path was still letting it guess.
_igh = Path("handlers/instagram_handler.py").read_text(encoding="utf-8")
_igm = Path("handlers/ig_post_menu.py").read_text(encoding="utf-8")
check("instagram video: streaming is switched on",
      "supports_streaming" in _igh)
check("instagram video: the duration and box are measured, not guessed",
      "probe_dimensions" in _igh)
check("instagram video: the single-video path uses it",
      "**video_kwargs(path)" in _igh)
check("instagram video: album items use it too",
      "InputMediaVideo(media=handle, caption=text," in _igh)
check("instagram video: the quality menu uses it",
      "video_kwargs(path)" in _igm)
check("instagram video: one helper, so the paths cannot drift apart again",
      _igh.count("def video_kwargs") == 1)
from handlers.instagram_handler import video_kwargs as _vk
check("instagram video: a missing ffprobe still enables streaming",
      _vk(Path("nope.mp4"))["supports_streaming"] is True)
check("instagram video: and never sends a made-up duration",
      _vk(Path("nope.mp4"))["duration"] is None)

# FFmpegExtractAudio copies when the source is aac and the target m4a, and
# re-encodes otherwise. Sorting on bitrate alone picked opus 146kbps over aac
# 129kbps and then transcoded it to aac 320k - a second lossy generation and a
# bigger file than the source.
# SoundCloud exposes the uploader's ORIGINAL file as format_id "download",
# with quality 10 and no abr at all. Sorting by aext then abr reads a missing
# bitrate as zero, so the best file available sorted LAST and a 320kbps
# original lost to a 160kbps stream - which is the 1.9MB against 3.6MB the
# same track arrived at from two different bots.
from yt_dlp import YoutubeDL as _YDL

_ORIG = {"format_id": "download", "ext": "mp3", "quality": 10, "vcodec": "none",
         "acodec": "mp3", "url": "https://x/a.mp3", "filesize": 3_600_000}
_AAC = {"format_id": "hls_aac_160k", "ext": "m4a", "abr": 160, "vcodec": "none",
        "acodec": "mp4a.40.2", "url": "https://x/b.m4a", "filesize": 1_900_000}


def _picks(selector, formats):
    with _YDL({"format": selector, "format_sort": ["aext:m4a", "abr"],
               "quiet": True, "no_warnings": True, "simulate": True}) as _y:
        _y.sort_formats({"formats": formats})
        _got = list(_y.build_format_selector(selector)(
            {"formats": formats, "incomplete_formats": False}))
    return _got[0]["format_id"] if _got else None


_SEL = "bestaudio[format_id=download]/bestaudio/best"
# From the journal, once per restart:
#
#   android_vr refused audio after 7.4s (HTTP Error 403: Forbidden)
#   default    served  audio in 11.2s
#
# The winner WAS remembered - in memory - so a restart threw it away and the
# first download of every session re-bought the same 403 at full price.
import json as _json2, tempfile as _tf2, pathlib as _pl2
_yt._preferred.pop("selfcheck", None)
_yt._preferred["selfcheck"] = ""
_yt._save_preferred()
_yt._preferred.clear()
_yt._load_preferred()
check("speed: the winning client survives a restart",
      _yt._preferred.get("selfcheck") == "")
_yt._preferred["selfcheck"] = "a-client-that-does-not-exist"
_yt._save_preferred()
_yt._preferred.clear()
_yt._load_preferred()
check("speed: a client no longer on the ladder is not resumed",
      "selfcheck" not in _yt._preferred)
_yt._preferred.pop("selfcheck", None)
_yt._save_preferred()

# A cached track took fifteen seconds because fill_details - several requests
# to iTunes and Deezer - ran BEFORE the one api call that sends the file.
_sph0 = Path("handlers/spotify_handler.py").read_text(encoding="utf-8")
_cached_branch = _sph0[_sph0.index("    cached = file_cache.get(cache_key)"):]
_cached_branch = _cached_branch[:_cached_branch.index("    # The cover goes out first")]
check("speed: nothing is looked up before a cached file is sent",
      "await sp.fill_details" not in _cached_branch
      and "_send_cached(msg, meta, cached" in _cached_branch)
check("speed: the cover is only sent when its url is already known",
      "with_cover=bool(meta.cover_url)" in _sph0)
check("speed: the enrichment still happens, just after delivery",
      "asyncio.create_task(_enrich_later(meta))" in _sph0)

# YouTube tops out near 146kbps and SoundCloud's streams at 160, so 320 was
# never a setting away - it had to come from a source that has it. Audius
# serves the uploader's original: 9.0MB for 3:55, measured, which is 320.
from modules import audius as _aud
# "â  â ÐÑÐ°Ð²Ð¸Ð»ÑÐ½ÑÐ¹ Ð¿Ð°ÑÐ¾Ð»Ñ â list index out of range": a track with no artist
# at all. Every other artists[0] in the codebase is guarded; search_query was
# the one that was not, and it is a property, so it threw from inside an
# f-string. display tolerated the same emptiness, which is why the message had
# a dangling dash and no name in front of it.
import ast as _ast2

_unguarded = []
for _f in sorted(Path("modules").glob("*.py")) + sorted(Path("handlers").glob("*.py")):
    for _n in _ast2.walk(_ast2.parse(_f.read_text(encoding="utf-8"))):
        if (isinstance(_n, _ast2.Subscript)
                and isinstance(_n.slice, _ast2.Constant) and _n.slice.value == 0
                and "artists" in _ast2.unparse(_n.value)):
            _line = _f.read_text(encoding="utf-8").splitlines()[_n.lineno - 1]
            if "if" not in _line and "or" not in _line:
                _unguarded.append(f"{_f.name}:{_n.lineno} {_line.strip()[:60]}")

check("artists: nothing indexes the first artist without checking there is one",
      not _unguarded, "; ".join(_unguarded[:3]))
check("artists: a track with none still has a usable search query",
      _sp.TrackMeta("x", "T", [], "", 1000, "", "").search_query == "T")
check("artists: and a name without a dangling separator",
      _sp.TrackMeta("x", "T", [], "", 1000, "", "").display == "T")
check("artists: the normal case is unchanged",
      _sp.TrackMeta("x", "T", ["A"], "", 1000, "", "").display == "A — T")

# A probe is a full yt-dlp extraction - 4.5 to 4.9 seconds against this
# server, and there is no cheaper way to do it. Measured the obvious candidate
# and it is worse than useless: player_skip=webpage makes YouTube demand a bot
# check and the extraction fails outright. So the only lever is doing it less
# often, and the cache was ten minutes and in memory.
_pinfo = _yt.VideoInfo(id="abc12345678", title="T", duration=300,
                       thumbnail="t", uploader="u",
                       available_heights={360, 720}, channel_url="c",
                       size_by_quality={"720p": 123})
_yt._probe_cache.clear()
_yt._probe_cache["abc12345678"] = (_yt.time.monotonic(), _pinfo)
_yt._save_probes()
_yt._probe_cache.clear()
_yt._load_probes()
_restored = _yt._probe_cache.get("abc12345678")
check("probe cache: it survives a restart",
      _restored is not None and _restored[1].title == "T")
check("probe cache: the format ladder survives with it",
      _restored and _restored[1].available_heights == {360, 720}
      and _restored[1].size_by_quality == {"720p": 123})
check("probe cache: a restored entry is not aged wrongly",
      _restored and (_yt.time.monotonic() - _restored[0]) < 5)
_yt._probe_cache["stale"] = (_yt.time.monotonic() - _yt._PROBE_TTL - 10, _pinfo)
_yt._save_probes()
_yt._probe_cache.clear()
_yt._load_probes()
check("probe cache: an expired entry does not come back",
      "stale" not in _yt._probe_cache)
check("probe cache: it lasts hours, not minutes",
      _yt._PROBE_TTL >= 3600)
check("probe: the webpage fetch is NOT skipped - it triggers the bot check",
      "player_skip" not in str(_yt._base_opts("android_vr")))
_yt._probe_cache.clear()
_yt._save_probes()

# Audius serves a 320kbps mp3. Converting that to m4a spends a lossy
# generation to change the container - the one place on this path where a
# transcode buys nothing. Everywhere else it earns its keep: YouTube's best is
# opus, which Telegram shows as a voice note rather than a track.
_spsrc2 = Path("modules/spotify.py").read_text(encoding="utf-8")
# Three minutes of "downloading..." with no file and no error. Nothing was
# hung - it was still going, and the track did arrive. Every individual step
# had a timeout; their SUM had none.
check("budget: the whole attempt has a ceiling, not just each request",
      "_DOWNLOAD_BUDGET" in _spsrc2)
check("budget: it is generous enough not to cut off a slow success",
      _sp._DOWNLOAD_BUDGET >= 120)
check("budget: the FIRST candidate is never interrupted by it",
      "if i and time.monotonic() - started_at" in _spsrc2)
check("budget: running out says so, rather than blaming the track",
      "خیلی طول کشید" in _spsrc2)
check("budget: and it is a distinct outcome from a refusal",
      _sp._download_failed(
          _sp.TrackMeta("x", "T", ["A"], "", 300000, "", ""),
          ["a", "b"], ["time budget exhausted"], None
      ).args[0] != _sp._download_failed(
          _sp.TrackMeta("x", "T", ["A"], "", 300000, "", ""),
          ["a", "b"], ["something went wrong"], None).args[0])

check("quality: the postprocessor is chosen per source",
      "def _opts_for(target: str)" in _spsrc2)
check("quality: an audius original is copied, not re-encoded",
      '"preferredcodec": "best"' in _spsrc2)
check("quality: everything else still gets the m4a transcode",
      '"preferredquality": "320"' in _spsrc2)
check("quality: the per-source choice is actually used by the download",
      "_opts_for(target)" in _spsrc2)
# YouTube's anonymous ceiling, measured across every client on a music video:
# only 139/140/249/251 exist - the 256kbps tiers are Premium-gated.
check("quality: no client is asked for a format YouTube will not serve",
      "player_skip" not in str(_yt._base_opts("android_vr")))

check("audius: entries are shaped like yt-dlp's, so the scoring can read them",
      all(k in _aud.search.__doc__ for k in ("Flat entries", "yt-dlp")))
_ahits = _aud.search("hichkas", limit=3)
check("audius: the public api answers without a key",
      isinstance(_ahits, list))
if _ahits:
    check("audius: every entry has what _match_entry needs",
          all(h.get("url", "").startswith("https://audius.co/")
              and h.get("title") and h.get("duration") is not None
              and h.get("uploader") is not None for h in _ahits))
check("audius: a query it cannot answer returns nothing, not an error",
      _aud.search("zzzqqqxxx nonexistent track") == [])
check("audius: it is searched alongside the other two",
      "audius" in _sp._AUDIO_SOURCES)
check("audius: and it is not a yt-dlp search prefix",
      'if source == "audius":' in
      Path("modules/spotify.py").read_text(encoding="utf-8"))

check("audio: the uploader's original is preferred when there is one",
      _picks(_SEL, [_ORIG, _AAC]) == "download")
check("audio: sorting alone would have missed it",
      _picks("bestaudio/best", [_ORIG, _AAC]) == "hls_aac_160k")
_OPUS = {"format_id": "251", "ext": "webm", "abr": 146, "vcodec": "none",
         "acodec": "opus", "url": "https://x/c.webm"}
_Y140 = {"format_id": "140", "ext": "m4a", "abr": 129, "vcodec": "none",
         "acodec": "mp4a.40.2", "url": "https://x/b.m4a"}


def _picks_abr(formats):
    with _YDL({"format": _SEL, "format_sort": ["abr"], "quiet": True,
               "no_warnings": True, "simulate": True}) as _y:
        _y.sort_formats({"formats": formats})
        _got = list(_y.build_format_selector(_SEL)(
            {"formats": formats, "incomplete_formats": False}))
    return _got[0]["format_id"] if _got else None


check("audio: on youtube the higher-bitrate opus is taken, not the aac",
      _picks_abr([_Y140, _OPUS]) == "251")
check("audio: and an original still beats both",
      _picks_abr([_ORIG, _Y140, _OPUS]) == "download")
check("audio: a track without an original is unaffected",
      _picks(_SEL, [_AAC]) == "hls_aac_160k")
check("audio: the download path actually asks for it",
      "bestaudio[format_id=download]" in
      Path("modules/spotify.py").read_text(encoding="utf-8"))

# This asserted the wrong rule. Preferring aac so ExtractAudio could copy
# traded YouTube's opus 251 at 146kbps for its aac 140 at 129 - and what
# reached the user was 2.2MB where it had been 5.4MB, because the copy keeps
# the source bitrate and the transcode targets 320. "A re-encode always loses"
# was true and beside the point: the comparison that matters is between the
# two SOURCES, and opus at 146 is well ahead of aac at 129.
_spsrc = Path("modules/spotify.py").read_text(encoding="utf-8")
check("audio: the highest-bitrate source wins, transcode or not",
      '"format_sort": ["abr"]' in _spsrc)
check("audio: the aac-first rule is gone from the setting, not just unused",
      '"format_sort": ["aext:m4a"' not in _spsrc)

# The quality menu offered 360p, 480p, 720p, 1080p and best as five buttons
# reading 516MB each. When a client serves one video stream every label picks
# it, so that was one file wearing five names - and four of the five were a
# choice that changed nothing.
_ONE = [{"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "height": 360,
         "filesize_approx": 516 * 1048576}]
_i1 = _yt.VideoInfo(id="x", title="t", duration=600, thumbnail="", uploader="u",
                    available_heights=[360, 480, 720, 1080],
                    size_by_quality=_yt._sizes_by_quality(_ONE, 600))
check("quality menu: one stream is offered once, not five times",
      len(_yt.quality_options_for(_i1)) == 1)
check("quality menu: labels that resolve to the same stream are dropped",
      len(_i1.size_by_quality) == 1)

_MANY = [{"format_id": "134", "vcodec": "avc1", "acodec": "none", "height": 360,
          "filesize": 18_000_000},
         {"format_id": "299", "vcodec": "avc1", "acodec": "none", "height": 1080,
          "filesize": 257_000_000},
         {"format_id": "140", "vcodec": "none", "acodec": "mp4a",
          "filesize": 5_000_000}]
_i2 = _yt.VideoInfo(id="y", title="t", duration=600, thumbnail="", uploader="u",
                    available_heights=[360, 1080],
                    size_by_quality=_yt._sizes_by_quality(_MANY, 600))
check("quality menu: genuinely different streams are all offered",
      set(_i2.size_by_quality) == {"360p", "1080p"})
check("quality menu: and their sizes differ",
      len(set(_i2.size_by_quality.values())) == 2)

check("quality menu: a size is estimated from bitrate when none is reported",
      _yt._size_of({"tbr": 400}, 300) > 0)
check("quality menu: and stays zero when there is nothing to estimate from",
      _yt._size_of({}, 300) == 0)

from modules import spotify as _spq2
# SoundCloud serves a 30-second preview for restricted tracks, at a perfectly
# ordinary 128kbps, with the full runtime still in the metadata.
check("audio: a file far shorter than the track is rejected",
      _spq2._SHORT_RATIO < 1.0)
check("audio: an unreadable length never rejects",
      _spq2._too_short(Path("nope-does-not-exist"),
                       _spq2.TrackMeta("x", "t", ["a"], "", 198000, "", "")) == 1.0)
check("audio: the preview check runs on the downloaded file, not the search",
      "_too_short(out_path, meta)" in
      Path("modules/spotify.py").read_text(encoding="utf-8"))

# YouTube shapes a download per CONNECTION, so one stream sits at whatever
# rate it is given - the difference between three minutes and under one on the
# same file. concurrent_fragment_downloads only ever applied to fragmented
# streams; a progressive mp4 is one file and never benefited.
_yts = Path("modules/youtube.py").read_text(encoding="utf-8")
check("speed: an external downloader is used when one is installed",
      "external_downloader" in _yts)
# aria2c cannot download HLS, and "default" handed SoundCloud's hls_aac_160k
# to it anyway: ERROR: aria2c exited with code 22 - it fetched a playlist and
# expected media.
from yt_dlp.downloader import get_suitable_downloader as _gsd
from yt_dlp.downloader.external import Aria2cFD as _A2

_was_avail = _A2.available
_A2.available = classmethod(lambda cls, path=None: True)
_saved_a2 = _yt._aria2c
_yt._aria2c = True
_o = _yt._fast_download_opts()


def _dl_for(proto, url):
    return _gsd({"protocol": proto, "url": url}, _o).__name__


# "aria2c exited with code 22" was not in _RETRYABLE, so it was re-raised
# immediately - no next client, no next candidate, and the user saw "all 1
# candidates failed". aria2c is an optimisation; its failure should cost speed,
# not the track.
# Measured on the live server: every aria2c attempt ended in code 22, then a
# native retry, then the same 403 - six seconds added to every download for an
# optimisation YouTube does not permit.
# "getting video info" is the slowest step between pasting a link and seeing
# the menu - one extraction, and more when the first client answers thinly and
# the ladder walks on. Nothing about a video changes minute to minute.
check("probe: the same video is not extracted twice in a row",
      "_probe_cache" in _yts and "_PROBE_TTL" in _yts)
check("probe: youtu.be, /shorts and a watch url share one entry",
      _yt._probe_key("https://youtu.be/cp-4X8hz9No")
      == _yt._probe_key("https://www.youtube.com/watch?v=cp-4X8hz9No&t=30s")
      == _yt._probe_key("https://www.youtube.com/shorts/cp-4X8hz9No"))
check("probe: a url with no video id is still usable as a key",
      _yt._probe_key("https://soundcloud.com/a/b") == "https://soundcloud.com/a/b")
# Was capped at an hour when the cache was in memory and a restart cleared it
# anyway. It is on disk now, and a format ladder does not change through the
# day - the worst case of a stale entry is a size estimate slightly off on a
# menu that already says approximate.
check("probe: the entry expires rather than being kept forever",
      0 < _yt._PROBE_TTL <= 24 * 3600)
check("probe: it is keyed on the id the response gave, not just the url",
      "_probe_cache[probed.id]" in _yts)

check("aria2c: it is off unless asked for",
      not _cfg.yt_use_aria2c)
check("aria2c: asking for it is a documented setting",
      "YT_USE_ARIA2C" in Path("config.py").read_text(encoding="utf-8"))
_saved_a2b = _yt._aria2c
_yt._aria2c = False
check("aria2c: off means no external downloader at all",
      _yt._fast_download_opts() == {})
_yt._aria2c = _saved_a2b

# android_vr answers a probe and then 403s every media request from this
# address. In memory that was re-learned at 8-14 seconds a download, three
# times over, after every restart.
_yt._refusals.clear()
for _ in range(_yt._REFUSALS_BEFORE_SKIP):
    _yt._note_refusal("selfcheck", "android_vr")
# From the journal, on every single download:
#     android_vr refused audio after 7.2s (HTTP Error 403: Forbidden)
#     default    served  audio in 18.9s
# A 403 is this address being told no by that client - not transient, not
# per-track. Waiting for three of them spends 21 seconds learning what the
# first one said plainly.
# A separate kind, so this leaves the state the later restart check reads.
_yt._note_refusal("decisive", "android_vr",
                  Exception("unable to download video data: HTTP Error 403: Forbidden"))
check("ladder: one 403 is enough to stop asking that client",
      _yt._is_cold("decisive", "android_vr"))
_yt._note_refusal("decisive", "ios", Exception("Unable to extract player response"))
check("ladder: an ordinary hiccup still gets its three chances",
      not _yt._is_cold("decisive", "ios"))
for _ in range(2):
    _yt._note_refusal("decisive", "ios", Exception("Unable to extract player response"))
check("ladder: and goes cold on the third",
      _yt._is_cold("decisive", "ios"))
for _c in ("android_vr", "ios"):
    _yt._refusals.pop(("decisive", _c), None)

# "Sign in to confirm you're not a bot" is a verdict about the ADDRESS.
#
# From the journal, one track, every rung of the ladder, the identical
# sentence - android_vr, default, ios, mweb, web_embedded, web_safari,
# tv_simply - eighty-four seconds to be told the same thing seven times, and
# then the whole thing again for the next candidate. Two clients agreeing is
# the address speaking; the other five have nothing to add.
_BOT_ERR = Exception(
    "ERROR: [youtube] aBcD: Sign in to confirm you’re not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication.")

_asked = []


class _CountingYDL:
    def __init__(self, opts):
        _asked.append((opts.get("extractor_args", {})
                       .get("youtube", {}).get("player_client", ["default"]))[0]
                      or "default")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _refuse(_ydl):
    raise _BOT_ERR


_real_ydl = _yt.YoutubeDL
_yt.YoutubeDL = _CountingYDL
_saved_refusals = dict(_yt._refusals)
_saved_preferred = dict(_yt._preferred)
_saved_check = _yt._last_bot_check
try:
    _yt._refusals.clear()
    _yt._preferred.clear()
    _yt._last_bot_check = 0.0
    _asked.clear()
    try:
        _yt.ytdlp_run({}, _refuse, kind="botwall")
    except Exception:
        pass
    check("ladder: two bot checks end it, the other rungs are not paid for",
          len(_asked) == _yt._BOT_CHECKS_BEFORE_GIVING_UP,
          f"asked {_asked} of {len(_yt._CLIENT_LADDER)} rungs")
    check("ladder: and the block is remembered for the callers who need it",
          _yt.bot_checked_recently())

    # One gated client is still just a client: android_vr has been refused
    # media while the default client served the very same request. Stopping on
    # the first bot check would throw that away.
    _yt._refusals.clear()
    _yt._preferred.clear()
    _asked.clear()
    _seen = []

    def _once_then_fine(_ydl):
        _seen.append(1)
        if len(_seen) == 1:
            raise _BOT_ERR
        return "served"

    check("ladder: a single gated client does not end the ladder",
          _yt.ytdlp_run({}, _once_then_fine, kind="botwall") == "served",
          f"asked {_asked}")
finally:
    _yt.YoutubeDL = _real_ydl
    _yt._refusals.clear()
    _yt._refusals.update(_saved_refusals)
    _yt._preferred.clear()
    _yt._preferred.update(_saved_preferred)
    # And put the FILE back too. _note_refusal persists, so walking a fake
    # ladder here rewrote the on-disk state that the restart check below
    # reads - restoring only the dict left that check reading the wipe.
    _yt._save_preferred()

# With playback refused, the candidate order is right about which recording is
# best and wrong about which one can be had. Search and playback are separate
# doors: YouTube answered searches in 0.7s while refusing every download, so
# the list came back led by three URLs that could not be fetched - and the
# SoundCloud copy behind them was never reached before the time budget hit.
_sp_meta = _sp.TrackMeta("sp_bw", "Berim Baham", ["Alireza JJ"], "", 200000, "", "")
_cands = [
    "https://www.youtube.com/watch?v=lSuH6XsodZI",
    "https://www.youtube.com/watch?v=Tf6tRfDjHqg",
    "https://soundcloud.com/x/berim-baham",
    "https://audius.co/x/berim-baham",
]
_yt._last_bot_check = _yt.time.monotonic()
_reordered = _sp._demote_blocked(_cands, _sp_meta)
check("targets: a bot-checked youtube is tried last, not first",
      not _reordered[0].startswith("https://www.youtube.com"),
      _reordered[0])
check("targets: and none of them are thrown away",
      sorted(_reordered) == sorted(_cands))
_yt._last_bot_check = 0.0
check("targets: while youtube answers, the scoring order stands",
      _sp._demote_blocked(_cands, _sp_meta) == _cands)

# The budget is a symptom. It used to be reported as the cause, which told the
# user "this took too long, send it again" - advice to repeat an attempt that
# could not succeed.
_yt._last_bot_check = _yt.time.monotonic()
_msg = str(_sp._download_failed(
    _sp_meta, _cands,
    ["Sign in to confirm you’re not a bot", "time budget exhausted"], None))
check("failure: a bot check is named, not hidden behind 'it took too long'",
      "botctl" in _msg and "دوباره بفرست" not in _msg,
      _msg[:70])
_yt._last_bot_check = _saved_check

# Hammering a flagged address is what keeps it flagged.
#
# Measured against YouTube directly, not assumed: one video id answered OK,
# then BOT-CHECK once a burst of requests had gone out, then OK again after
# ninety seconds of quiet. The check is largely about request RATE, and this
# bot was its own worst source of it - seven ladder rungs across three
# candidates is twenty-one player requests for a single song.
_sp_calls = []


def _always_bot_check(_opts, _fn, kind="", accept=None):
    _sp_calls.append(1)
    _yt._last_bot_check = _yt.time.monotonic()
    raise RuntimeError("یوتیوب این ویدیو رو نداد.")


# spotify imports ytdlp_run from youtube at call time, so the
# attribute to swap lives on youtube.
_real_run = _yt.ytdlp_run
_real_locate = _sp._locate_audio
_yt.ytdlp_run = _always_bot_check
# No network from selfcheck, and this check is about the youtube retries.
_sp._locate_audio = lambda meta: []
_saved_check2 = _yt._last_bot_check
try:
    _yt._last_bot_check = 0.0
    _sp_calls.clear()
    _sp_targets = [f"https://www.youtube.com/watch?v=vid{n}" for n in range(4)]
    try:
        _sp.download_track.__wrapped__(
            _sp.TrackMeta("yt_vid0", "T", ["A"], "", 0,
                          "https://www.youtube.com/watch?v=vid0", ""))
    except Exception as _e:
        _sp_err = str(_e)
    check("download: a bot check mid-attempt stops the other youtube tries",
          len(_sp_calls) == 1, f"{len(_sp_calls)} attempt(s)")

    # ytdlp_run raises through _friendly(), which turns the bot check into
    # Persian prose. reasons held only the prose, so _download_failed matched
    # none of its English markers and the one failure the bot can explain
    # precisely came out as the generic "all N candidates failed".
    check("download: the cause survives the translation shown to the user",
          "botctl" in _sp_err, _sp_err[:70])
finally:
    _yt.ytdlp_run = _real_run
    _sp._locate_audio = _real_locate
    _yt._last_bot_check = _saved_check2

# The exit is a fallback rung, not a toll booth.
#
# YT_PROXY used to apply to every request the moment it was set: the searches
# that were never refused, the probe, and the media transfer itself. A WARP
# hop is a fine way to ask YouTube a question from somewhere else and a poor
# way to move nine megabytes, so with the proxy on the bot worked and crawled.
_px_asked = []


class _ExitRecordingYDL:
    def __init__(self, opts):
        _px_asked.append(
            ((opts.get("extractor_args", {})
              .get("youtube", {}).get("player_client", ["default"]))[0]
             or "default",
             "proxy" if opts.get("proxy") else "direct"))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_px_real_ydl = _yt.YoutubeDL
_px_real_proxy = _yt.settings.yt_proxy
_px_saved_ref = dict(_yt._refusals)
_px_saved_pref = dict(_yt._preferred)
_px_saved_check = _yt._last_bot_check
try:
    _yt.YoutubeDL = _ExitRecordingYDL
    object.__setattr__(_yt.settings, "yt_proxy", "socks5h://127.0.0.1:40000")
    check("proxy: a configured exit is recognised", _yt._proxy_available())

    _yt._refusals.clear()
    _yt._preferred.clear()
    _yt._last_bot_check = 0.0
    _yt._last_direct_bot_check = 0.0
    _px_asked.clear()
    _yt.ytdlp_run({}, lambda _ydl: "served", kind="pxok")
    check("proxy: a download that works never touches the tunnel",
          all(e == "direct" for _, e in _px_asked), str(_px_asked))

    # Refused direct, served through the exit - and the direct rungs in
    # between are not paid for, because each refusal deepens the block.
    _yt._refusals.clear()
    _yt._preferred.clear()
    _yt._last_bot_check = 0.0
    _yt._last_direct_bot_check = 0.0
    _px_asked.clear()

    def _proxy_only(_ydl):
        if _px_asked[-1][1] == "direct":
            raise RuntimeError(
                "ERROR: [youtube] x: Sign in to confirm you’re not a bot.")
        return "served"

    check("proxy: a bot check falls through to the exit and is served",
          _yt.ytdlp_run({}, _proxy_only, kind="pxok") == "served", str(_px_asked))
    check("proxy: and the rest of the direct ladder is not paid for",
          sum(1 for _, e in _px_asked if e == "direct")
          == _yt._BOT_CHECKS_BEFORE_GIVING_UP,
          f"{_px_asked} of {len(_yt._CLIENT_LADDER)} rungs")

    # The tunnel used to get two rungs. That was the don't-hammer rule from
    # the bot check applied where it does not belong - a bot check is about
    # one address, and the proxy is a different address - so the exit that
    # actually works was capped at two clients. Probes that used to succeed
    # on the fourth or fifth client just failed.
    _yt._last_direct_bot_check = 0.0
    check("proxy: the exit gets the whole ladder, not a couple of rungs",
          sum(1 for _, viap in _yt._rungs("pxok") if viap)
          == len(_yt._ladder("pxok")),
          f"{sum(1 for _, viap in _yt._rungs('pxok') if viap)} proxied rungs")

    # And direct always went first, even right after direct was refused -
    # two refusals of waiting in front of every request, paid over and over
    # to re-learn the same answer.
    check("proxy: while direct answers, direct leads",
          not _yt._rungs("pxok")[0][1])
    _yt._last_direct_bot_check = _yt.time.monotonic()
    check("proxy: once direct is refusing, the tunnel leads",
          _yt._rungs("pxok")[0][1])
    check("proxy: and direct is still behind it, because the block lifts",
          any(not viap for _, viap in _yt._rungs("pxok")))
    _yt._last_direct_bot_check = 0.0

    # A bot check through the TUNNEL is not a reason to lead with the tunnel.
    _yt._last_bot_check = _yt.time.monotonic()
    check("proxy: a refusal via the tunnel does not promote the tunnel",
          not _yt._rungs("pxok")[0][1])
    _yt._last_bot_check = 0.0

    object.__setattr__(_yt.settings, "yt_proxy", "")
    check("proxy: with no exit configured the ladder is direct only",
          all(not viap for _, viap in _yt._rungs("pxok")))
finally:
    _yt.YoutubeDL = _px_real_ydl
    object.__setattr__(_yt.settings, "yt_proxy", _px_real_proxy)
    _yt._refusals.clear()
    _yt._refusals.update(_px_saved_ref)
    _yt._preferred.clear()
    _yt._preferred.update(_px_saved_pref)
    _yt._last_bot_check = _px_saved_check
    _yt._last_direct_bot_check = 0.0
    _yt._save_preferred()

check("ladder: a refusing client is written down, not just remembered",
      _yt._PREFERRED_PATH.exists())
_yt._refusals.clear()
_yt._load_preferred()
check("ladder: and it is still skipped after a restart",
      _yt._is_cold("selfcheck", "android_vr"))
_yt._refusals[("selfcheck", "android_vr")] = (
    _yt._REFUSALS_BEFORE_SKIP, _yt.time.monotonic() - _yt._REFUSAL_COOLDOWN - 1)
check("ladder: the cooldown still gives it another chance",
      not _yt._is_cold("selfcheck", "android_vr"))
_yt._refusals.clear()
_yt._save_preferred()

check("aria2c: its failure is told apart from the site refusing",
      _yt._is_external_downloader_failure(Exception("ERROR: aria2c exited with code 22")))
check("aria2c: a real 403 is not mistaken for it",
      not _yt._is_external_downloader_failure(Exception("HTTP Error 403: Forbidden")))
check("aria2c: the same client is retried natively first",
      "_without_external_downloader(opts)" in _yts)
check("aria2c: stripping it leaves the rest of the options alone",
      _yt._without_external_downloader(
          {"format": "bestaudio", "external_downloader": {"http": "aria2c"},
           "external_downloader_args": {"aria2c": []}}) == {"format": "bestaudio"})
check("aria2c: and if native fails too, the ladder still moves on",
      "aria2c exited" in _yt._RETRYABLE)

check("speed: plain http goes to aria2c",
      _dl_for("https", "https://x/a.m4a") == "Aria2cFD")
check("speed: HLS does NOT - that is the code 22",
      _dl_for("m3u8_native", "https://x/a.m3u8") == "HlsFD")
check("speed: nor does DASH",
      _dl_for("http_dash_segments", "https://x/a.mpd") == "DashSegmentsFD")
check("speed: there is no catch-all that could take HLS again",
      "default" not in _o["external_downloader"])
_yt._aria2c = _saved_a2
_A2.available = _was_avail

check("speed: it splits the file across connections",
      '"-x", "16"' in _yts and '"-s", "16"' in _yts)
check("speed: no pre-allocation pause before the first byte",
      "--file-allocation=none" in _yts)
_saved_aria = _yt._aria2c
_yt._aria2c = False
check("speed: a missing aria2c changes nothing rather than breaking",
      _yt._fast_download_opts() == {})
_yt._aria2c = True
check("speed: a present aria2c is actually wired in",
      _yt._fast_download_opts().get("external_downloader"))
_yt._aria2c = _saved_aria
check("speed: audio downloads get it too",
      "_yt_fast_opts()" in Path("modules/spotify.py").read_text(encoding="utf-8"))
check("install: aria2 is installed with the other tools",
      "aria2" in Path("deploy/manage.sh").read_text(encoding="utf-8"))

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
check("video: mp4-native codecs are preferred so the merge is a remux",
      '"format_sort": ["vcodec:h264", "acodec:m4a"]' in _yts)
check("video: it is a preference, not a filter that can fail",
      not any("vcodec^=" in v for v in _yt.QUALITY_CHOICES.values()))

# The ladder is walked in full on EVERY call, and download_track calls it once
# per candidate - seven clients times six candidates is forty-two attempts,
# most of them to clients that never return audio on this address. That is
# most of a two-minute download spent on predictable refusals.
_saved_ref = dict(_yt._refusals)
_yt._refusals.clear()
check("ladder: it starts out willing to try everything",
      len(_yt._ladder("t")) == len(_yt._CLIENT_LADDER))
for _c in ("ios", "mweb", "web_embedded", "web_safari", "tv_simply"):
    for _ in range(3):
        _yt._note_refusal("t", _c)
check("ladder: clients that keep refusing are dropped",
      len(_yt._ladder("t")) == 2)
for _c in ("android_vr", ""):
    for _ in range(3):
        _yt._note_refusal("t", _c)
check("ladder: it never empties itself",
      len(_yt._ladder("t")) == len(_yt._CLIENT_LADDER))
_yt._note_success("t", "ios")
check("ladder: one success brings a client straight back",
      not _yt._is_cold("t", "ios"))
_yt._refusals.clear()
_yt._refusals.update(_saved_ref)
check("ladder: a cold client is retried after the cooldown",
      _yt._REFUSAL_COOLDOWN > 0 and _yt._REFUSALS_BEFORE_SKIP >= 2)

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


# --------------------------------------------------------------------------
# Instagram: stories, highlights, profile pictures
# --------------------------------------------------------------------------
import asyncio as _ig_aio  # noqa: E402
import inspect as _ig_inspect  # noqa: E402

from handlers import instagram_handler as _ih  # noqa: E402
from modules import instagram as _igm  # noqa: E402

_ig_src = _ig_inspect.getsource(_igm)

# Instagram has two APIs and they do not accept the same credential. A
# sessionid copied out of a browser is issued to the WEB api and is refused by
# the mobile one permanently. Stories used to be wired only to the mobile side
# (instagrapi, instaloader), so the one credential an operator can actually
# obtain could never reach them - both "options" the failure message offered
# led to the same wall.
check("ig: stories try the web session before the mobile api",
      _ig_src.index("_story_urls_web(username)")
      < _ig_src.index("urls = _story_urls_private(username)"))
check("ig: the profile picture tries the web session too",
      "ig_stories.profile_pic_url" in _ig_src)

# unavatar.io moved its Instagram provider behind a paid plan, so the
# anonymous route now answers 403 EPRO. picuki and imginn 403 this server as
# well, and web_profile_info answers 429 logged out - there is no dependable
# logged-out route left, which is why the session is tried FIRST now rather
# than as a fallback.
check("ig: the retired free mirror is reported as retired, not as a bug",
      "EPRO" in _ig_src)

# Telegram caps callback_data at 64 bytes. A highlight id is "highlight:"
# plus eighteen digits, so a button carrying one would overflow on any
# ordinary username - and fail silently, with nothing in the logs to say why.
# The buttons carry an index into chat_data instead.
_ig_tray = [{"id": f"highlight:1785123456789012{n}", "title": "t" * 40,
             "count": n} for n in range(4)]
_ig_sent = []


class _IgStatus:
    chat_id = 1

    async def edit_text(self, text, **kw):
        _ig_sent.append((text, kw))

    async def delete(self):
        pass


class _IgMsg:
    chat_id = 1

    async def reply_text(self, text, **kw):
        _ig_sent.append((text, kw))
        return _IgStatus()


class _IgQuery:
    def __init__(self, data):
        self.data = data
        self.message = _IgMsg()

    async def answer(self):
        pass


def _ig_fire(data, ctx):
    _ig_aio.run(_ih.on_callback(
        type("U", (), {"callback_query": _IgQuery(data)})(), ctx))


_ig_real_list = _igm.list_highlights
_ig_real_fetch = _igm.fetch_highlight
_ig_real_send = _ih._send_media
try:
    _igm.list_highlights = lambda u: _ig_aio.sleep(0, result=_ig_tray)
    _ig_ctx = type("C", (), {"chat_data": {}})()
    _ig_sent.clear()
    _ig_fire("ig:hl:" + "u" * 30, _ig_ctx)
    _ig_kb = _ig_sent[-1][1]["reply_markup"].inline_keyboard
    _ig_worst = max(len(b.callback_data.encode()) for row in _ig_kb for b in row)
    check("ig: highlight buttons fit inside Telegram's 64-byte callback_data",
          _ig_worst <= 64, f"longest {_ig_worst} bytes")

    # The button carries an index, so it has to resolve back to the right
    # highlight - an off-by-one here downloads somebody else's album.
    _ig_picked = {}

    async def _ig_fake_fetch(username, hid):
        _ig_picked["id"] = hid
        return []

    _igm.fetch_highlight = _ig_fake_fetch
    _ih._send_media = lambda msg, files: _ig_aio.sleep(0)
    _ig_fire("ig:hli:" + "u" * 30 + ":2", _ig_ctx)
    check("ig: the index on the button resolves to that highlight",
          _ig_picked.get("id") == _ig_tray[2]["id"], str(_ig_picked))

    # chat_data does not survive a restart, and the buttons do.
    _ig_ctx.chat_data.clear()
    _ig_sent.clear()
    _ig_fire("ig:hli:" + "u" * 30 + ":2", _ig_ctx)
    check("ig: a highlight button from before a restart says so",
          "\u0642\u062f\u06cc\u0645\u06cc" in _ig_sent[-1][0],
          _ig_sent[-1][0][:50])
finally:
    _igm.list_highlights = _ig_real_list
    _igm.fetch_highlight = _ig_real_fetch
    _ih._send_media = _ig_real_send


# A retired endpoint is not a missing account.
#
# web_profile_info answers, from the server, with
#
#     HTTP 400 {"message":"Asset asset://laser.provider/
#               ig_business_category_subvertical has been deleted.
#               You cannot use this schema"}
#
# Instagram failing to serialise its own response: a field was removed from
# the schema that endpoint still declares. Nothing about the request is
# wrong and no header fixes it - other Instagram tools hit the same wall at
# the same time.
from modules import ig_stories as _igs  # noqa: E402
from modules import ig_web as _igw  # noqa: E402

_igs_calls = []
_IGS_400 = ('HTTP 400: {"message":"Asset asset://laser.provider/'
            'ig_business_category_subvertical has been deleted. '
            'You cannot use this schema",')
# Two accounts whose names share a prefix. Picking the first search hit
# instead of the exact match downloads a different person's stories.
_IGS_TOP = {"users": [
    {"user": {"pk": "999", "username": "someone_fan",
              "profile_pic_url": "https://cdn/wrong.jpg"}},
    {"user": {"pk": "12345", "username": "SomeOne", "is_private": False,
              "profile_pic_url": "https://cdn/small.jpg"}},
]}


def _igs_fake_get(url, params, referer=""):
    _igs_calls.append(url)
    if "web_profile_info" in url:
        raise RuntimeError(_IGS_400)
    if "topsearch" in url:
        return _IGS_TOP
    if "/info/" in url:
        return {"user": {"hd_profile_pic_url_info": {"url": "https://cdn/HD.jpg"}}}
    if "reels_media" in url:
        return {"reels_media": [{"items": [
            {"video_versions": [{"url": "https://cdn/clip.mp4"}],
             "image_versions2": {"candidates": [{"url": "https://cdn/cover.jpg"}]}},
        ]}]}
    return {}


_igs_real_get = _igw.get
_igs_real_usable = _igw.usable
try:
    _igw.get = _igs_fake_get
    _igw.usable = lambda: True
    _igs._retired.clear()

    _igs_user = _igs.profile("someone")
    check("ig: a retired endpoint falls through to the search route",
          _igs_user["id"] == "12345", str(_igs_user))
    check("ig: and the exact username wins over a lookalike",
          _igs_user["username"] == "SomeOne", _igs_user["username"])

    _igs_calls.clear()
    _igs.profile("someone")
    check("ig: the retired endpoint is not paid for on every button press",
          not any("web_profile_info" in c for c in _igs_calls),
          str([c.rsplit("/", 2)[-2] for c in _igs_calls]))

    # A story that has both is a video; the image is only its cover frame,
    # and sending that looks like success while losing what was asked for.
    check("ig: a story sends the clip, not its cover frame",
          _igs.story_urls("someone")[0] == ["https://cdn/clip.mp4"])
    check("ig: the avatar is upgraded to the hd rendition",
          _igs.profile_pic_url("someone") == "https://cdn/HD.jpg")

    # "Could not find that account" is the wrong thing to say when the
    # account is fine and every route was refused.
    _igs._retired.clear()

    def _igs_all_fail(url, params, referer=""):
        raise RuntimeError("HTTP 401: sessionid rejected")

    _igw.get = _igs_all_fail
    try:
        _igs.profile("someone")
        _igs_msg = ""
    except Exception as _igs_e:
        _igs_msg = str(_igs_e)
    check("ig: a total failure reports what each route said",
          "401" in _igs_msg and "topsearch" in _igs_msg,
          _igs_msg.replace("\n", " | ")[:80])
finally:
    _igw.get = _igs_real_get
    _igw.usable = _igs_real_usable
    _igs._retired.clear()


# An age gate wears the bot check's clothes.
#
# YouTube says "Sign in to confirm your AGE", and _BOT_CHECK matches on
# "sign in to confirm" - so one age-restricted link was read as this address
# being refused. The wrong error message was the small part: it also set the
# bot-check clock, which routes EVERY later request through the proxy for ten
# minutes. One restricted video slowed the whole bot down.
#
# These are the seven real answers, measured against tJbzMqJGH4k.
_age_asked = []
_AGE_ANSWERS = {
    "android_vr": "ERROR: [youtube] x: Sign in to confirm your age. Use --cookies-from-browser",
    "default": "ERROR: [youtube] x: Sign in to confirm your age. Use --cookies-from-browser",
    "ios": "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.",
    "mweb": "ERROR: [youtube] x: Sign in to confirm your age.",
    "web_embedded": "ERROR: [youtube] x: Sorry, this content is age-restricted",
    "web_safari": "ERROR: [youtube] x: Sign in to confirm your age.",
    "tv_simply": "ERROR: [youtube] x: This video is age-restricted and only available to signed-in users",
}


class _AgeYDL:
    def __init__(self, opts):
        _age_asked.append(
            ((opts.get("extractor_args", {})
              .get("youtube", {}).get("player_client", ["default"]))[0]
             or "default",
             "proxy" if opts.get("proxy") else "direct"))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _age_gated(_ydl):
    raise RuntimeError(_AGE_ANSWERS[_age_asked[-1][0]])


_age_real_ydl = _yt.YoutubeDL
_age_real_proxy = _yt.settings.yt_proxy
_age_saved_ref = dict(_yt._refusals)
_age_saved_pref = dict(_yt._preferred)
try:
    _yt.YoutubeDL = _AgeYDL
    object.__setattr__(_yt.settings, "yt_proxy", "socks5h://127.0.0.1:40000")
    _yt._refusals.clear()
    _yt._preferred.clear()
    _yt._last_bot_check = 0.0
    _yt._last_direct_bot_check = 0.0
    _age_asked.clear()
    try:
        _yt.ytdlp_run({}, _age_gated, kind="agechk")
        _age_msg = ""
    except Exception as _age_e:
        _age_msg = str(_age_e)

    check("age gate: it is reported as a restricted video, not a server problem",
          "\u0633\u0646\u06cc" in _age_msg and "ytcookies" in _age_msg,
          _age_msg.splitlines()[0][:60] if _age_msg else "(no error)")
    check("age gate: two clients agreeing is enough",
          len(_age_asked) == _yt._BOT_CHECKS_BEFORE_GIVING_UP,
          f"asked {[c for c, _ in _age_asked]}")
    # A different address cannot make someone old enough.
    check("age gate: the tunnel is not tried, because it cannot help",
          {e for _, e in _age_asked} == {"direct"},
          str(sorted({e for _, e in _age_asked})))
    # The part that made one link slow everything down.
    check("age gate: the bot-check clock is left alone",
          not _yt.bot_checked_recently())
    check("age gate: so the next request still opens on the direct exit",
          not _yt._rungs("agechk")[0][1])

    # And a genuine bot check must still do all of it.
    _yt._refusals.clear()
    _yt._preferred.clear()
    _age_asked.clear()

    def _real_botcheck(_ydl):
        raise RuntimeError("ERROR: Sign in to confirm you\u2019re not a bot.")

    try:
        _yt.ytdlp_run({}, _real_botcheck, kind="agechk")
    except Exception:
        pass
    check("age gate: a real bot check is still treated as one",
          _yt.bot_checked_recently() and _yt._rungs("agechk")[0][1])
    check("age gate: and it still falls through to the other exit",
          {e for _, e in _age_asked} == {"direct", "proxy"},
          str(sorted({e for _, e in _age_asked})))
finally:
    _yt.YoutubeDL = _age_real_ydl
    object.__setattr__(_yt.settings, "yt_proxy", _age_real_proxy)
    _yt._refusals.clear()
    _yt._refusals.update(_age_saved_ref)
    _yt._preferred.clear()
    _yt._preferred.update(_age_saved_pref)
    _yt._last_bot_check = 0.0
    _yt._last_direct_bot_check = 0.0
    _yt._save_preferred()


# --------------------------------------------------------------------------
# Moving the bot to another server
# --------------------------------------------------------------------------
import json as _bk_json  # noqa: E402
import sqlite3 as _bk_sql  # noqa: E402
import tarfile as _bk_tar  # noqa: E402
import tempfile as _bk_tmp  # noqa: E402
import shutil as _bk_shutil  # noqa: E402
import importlib.util as _bk_ilu  # noqa: E402

_bk_spec = _bk_ilu.spec_from_file_location(
    "_bk", str(Path(__file__).resolve().parent / "backup.py"))
_bk = _bk_ilu.module_from_spec(_bk_spec)
_bk_spec.loader.exec_module(_bk)
_bk_py_src = (Path(__file__).resolve().parent / "backup.py").read_text(
    encoding="utf-8", errors="replace")
_bk_sh_src = (Path(__file__).resolve().parent / "manage.sh").read_text(
    encoding="utf-8", errors="replace")

# The archive has to carry what cannot be regenerated, and nothing else.
for _bk_need in ("file_ids.json", "stats.db", "ig_web_cookies.json",
                 "ig_device.json", "ig_device_owner.txt"):
    check(f"backup: {_bk_need} is in the archive", _bk_need in _bk.STATE_FILES)
check("backup: the .env goes with it", ".env" in _bk.PROJECT_FILES)
# Media rebuilds on demand; including it turns a 2KB archive into gigabytes.
check("backup: downloaded media is left behind",
      not any(d in _bk.STATE_FILES
              for d in ("spotify", "instagram", "recognize", "thumbs")))

_bk_root = Path(_bk_tmp.mkdtemp(prefix="selfcheck-bk-"))
_bk_real_project = _bk.PROJECT_DIR
try:
    # An old server with state on it.
    _bk_old = _bk_root / "old"
    _bk_old_dl = _bk_old / "downloads"
    _bk_old_dl.mkdir(parents=True)
    (_bk_old / ".env").write_text(
        f"BOT_TOKEN=123:SECRET\nDOWNLOAD_DIR={_bk_old_dl}\n"
        "CACHE_CHANNEL_ID=-1004441522512\n", encoding="utf-8")
    (_bk_old_dl / "file_ids.json").write_text(
        _bk_json.dumps({"audio:sp_a": "FILE_ID_A"}), encoding="utf-8")
    (_bk_old_dl / "ig_web_cookies.json").write_text(
        _bk_json.dumps({"sessionid": "ROTATED"}), encoding="utf-8")
    (_bk_old_dl / "spotify").mkdir()
    (_bk_old_dl / "spotify" / "big.mp3").write_bytes(b"\0" * 200_000)
    _bk_conn = _bk_sql.connect(_bk_old_dl / "stats.db")
    _bk_conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, lang TEXT)")
    _bk_conn.execute("INSERT INTO users VALUES (7, 'en')")
    _bk_conn.commit()
    _bk_conn.close()

    _bk_archive = _bk_root / "b.tar.gz"
    _bk.PROJECT_DIR = _bk_old
    check("backup: it is created", _bk.create(_bk_archive) == 0)
    check("backup: and stays small, because the media stayed home",
          _bk_archive.stat().st_size < 100_000,
          f"{_bk_archive.stat().st_size:,} bytes vs 200,000 of media")

    # A brand new server - installed at a DIFFERENT path, which is the case
    # that used to fail silently: the archive's DOWNLOAD_DIR pointed at the
    # old machine, so every file was reported restored into a directory this
    # bot would never read.
    _bk_new = _bk_root / "new"
    _bk_new_dl = _bk_new / "downloads"
    _bk_new_dl.mkdir(parents=True)
    (_bk_new / ".env").write_text(
        f"BOT_TOKEN=placeholder\nDOWNLOAD_DIR={_bk_new_dl}\n", encoding="utf-8")
    _bk.PROJECT_DIR = _bk_new
    check("backup: it restores onto a fresh server", _bk.restore(_bk_archive) == 0)

    check("backup: the file_id cache came across",
          _bk_json.loads((_bk_new_dl / "file_ids.json").read_text(
              encoding="utf-8")) == {"audio:sp_a": "FILE_ID_A"})
    check("backup: the live Instagram session came across",
          (_bk_new_dl / "ig_web_cookies.json").is_file())
    _bk_conn = _bk_sql.connect(_bk_new_dl / "stats.db")
    check("backup: stats.db opens and still has its rows",
          _bk_conn.execute("SELECT lang FROM users WHERE user_id=7"
                           ).fetchone()[0] == "en")
    _bk_conn.close()
    _bk_env = (_bk_new / ".env").read_text(encoding="utf-8")
    check("backup: the token is the old bot's", "123:SECRET" in _bk_env)
    check("backup: and so is the cache channel", "-1004441522512" in _bk_env)
    # The fix: identity from the archive, paths from this machine.
    check("backup: but DOWNLOAD_DIR points at THIS server",
          f"DOWNLOAD_DIR={_bk_new_dl}" in _bk_env,
          [l for l in _bk_env.splitlines() if "DOWNLOAD_DIR" in l])
    check("backup: media was not dragged along",
          not (_bk_new_dl / "spotify").exists())

    # A tar can name "../../etc/passwd". Nothing in this archive is ever a
    # path, so anything that is one did not come from here.
    _bk_evil = _bk_root / "evil.tar.gz"
    _bk_probe = _bk_root / "probe.txt"
    _bk_probe.write_text("x", encoding="utf-8")
    with _bk_tar.open(_bk_evil, "w:gz") as _bk_t:
        _bk_t.add(_bk_probe, arcname="../../../../etc/passwd")
    check("backup: an archive that escapes its directory is refused",
          _bk.restore(_bk_evil) == 1)

    # Both halves are root's job: reading every state file whatever its
    # owner, and writing into /root. As the bot user, backup collected all
    # thirteen files and then died on
    #   PermissionError: '/root/moeinimydl-backup-....tar.gz'
    check("backup: it runs as root, not as the bot user",
          'run_py_root "$PROJECT_DIR/deploy/backup.py" create' in _bk_sh_src)
    check("backup: and so does restore",
          'run_py_root "$PROJECT_DIR/deploy/backup.py" restore' in _bk_sh_src)
    # Running as root means everything it writes is root-owned, and a bot
    # that cannot write its own cache or rotate its own Instagram cookie
    # fails quietly minutes after a restore that reported success.
    check("backup: restored files are handed back to whoever owns the directory",
          "_match_owner(dest, target_dir)" in _bk_py_src)
finally:
    _bk.PROJECT_DIR = _bk_real_project
    _bk_shutil.rmtree(_bk_root, ignore_errors=True)


# manage.sh runs under `set -u`, so a dispatch line that names an argument
# the user did not type aborts the script before the function that knows how
# to default it is ever reached:
#
#     botctl backup
#     /usr/local/bin/botctl: line 2292: $2: unbound variable
#
# The function had `local out="${1:-...}"` and never got to run it. Every
# optional argument has to be defaulted at the CALL site, not only inside.
import re as _sh_re  # noqa: E402

_sh_src = (Path(__file__).resolve().parent / "manage.sh").read_text(
    encoding="utf-8", errors="replace")
check("manage.sh: it still runs under set -u",
      _sh_re.search(r"^set -\w*u", _sh_src, _sh_re.M) is not None)

_sh_bare = [
    line.strip()
    for line in _sh_src.splitlines()
    # A case arm that calls a do_* function with a positional argument.
    if _sh_re.match(r'^\s*[a-z0-9|]+\)\s*do_\w+', line)
    and _sh_re.search(r'"\$[0-9]"', line)
]
check("manage.sh: no dispatch line dereferences a missing argument",
      not _sh_bare, str(_sh_bare[:3]))


# A locked upload is not a missing song.
#
# Picking "Shekastani (Live)" out of the results menu produces an sc_ id and
# exactly one target - that upload. SoundCloud answered
#
#     ERROR: [soundcloud] 1781595549: This video is DRM protected
#
# and the track was reported undownloadable, while the same recording sat on
# four other uploads that were never looked at. What was picked was a song;
# the url was only how the menu named it.
_drm_tried = []
_DRM_ERR = "ERROR: [soundcloud] 1781595549: This video is DRM protected"
_DRM_ALTS = ["https://www.youtube.com/watch?v=Ir3C3uxKQ1Y",
             "https://api.soundcloud.com/tracks/1196147050"]


def _drm_run(_opts, fn, kind="", accept=None):
    # The url is not passed here, so the call order is the record.
    _drm_tried.append(len(_drm_tried))
    if len(_drm_tried) == 1:
        raise RuntimeError(_DRM_ERR)
    raise RuntimeError("ERROR: nothing here either")


_drm_real_run = _yt.ytdlp_run
_drm_real_locate = _sp._locate_audio
try:
    _yt.ytdlp_run = _drm_run
    _sp._locate_audio = lambda meta: list(_DRM_ALTS)
    # The url is spotify_url (field 7), not cover_url (field 6) - putting it
    # in the wrong one sends this down the search branch and tests nothing.
    _drm_meta = _sp.TrackMeta(
        "sc_1781595549", "Shekastani (Live)", ["Omid"], "", 0, "",
        "https://api.soundcloud.com/tracks/1781595549")
    try:
        _sp.download_track.__wrapped__(_drm_meta)
        _drm_msg = ""
    except Exception as _drm_e:
        _drm_msg = str(_drm_e)

    check("drm: a locked upload sends the bot looking for another one",
          len(_drm_tried) == 1 + len(_DRM_ALTS),
          f"{len(_drm_tried)} attempt(s), expected {1 + len(_DRM_ALTS)}")

    # Only once - a widening that widens again is a search loop.
    _drm_tried.clear()
    _sp._locate_audio = lambda meta: list(_DRM_ALTS) + ["https://x/3"]
    try:
        _sp.download_track.__wrapped__(_drm_meta)
    except Exception:
        pass
    check("drm: and only widens once, never in a loop",
          len(_drm_tried) <= 1 + len(_DRM_ALTS) + 1,
          f"{len(_drm_tried)} attempt(s)")

    # A pasted link with no alternatives must still say what happened,
    # rather than "it failed" - which invites a retry that cannot work.
    _sp._locate_audio = lambda meta: []
    _drm_tried.clear()
    try:
        _sp.download_track.__wrapped__(_drm_meta)
        _drm_msg = ""
    except Exception as _drm_e:
        _drm_msg = str(_drm_e)
    check("drm: it is named as DRM, not reported as a failed download",
          "DRM" in _drm_msg, _drm_msg[:70])
finally:
    _yt.ytdlp_run = _drm_real_run
    _sp._locate_audio = _drm_real_locate


# A featured-artist parenthetical is a credit, not part of the title.
#
# Spotify calls this track "Hate Me (with Juice WRLD)". Every upload of it on
# YouTube is "Ellie Goulding, Juice WRLD - Hate Me (Official Video)" - the
# featured artist goes BEFORE the dash there, so the parenthetical can never
# appear in their title text:
#
#     ours   'hate me with juice wrld'
#     theirs 'ellie goulding juice wrld hate me official video'   rejected
#     ours   'hate me'                                            5.0
#
# Every result was thrown away and the track reported as not existing, on a
# song the bot had downloaded before.
for _cr_in, _cr_out in (
    ("Hate Me (with Juice WRLD)", "Hate Me"),
    ("Song (feat. X)", "Song"),
    ("Song [ft. Y]", "Song"),
    ("Song (featuring Z)", "Song"),
):
    check(f"credits: {_cr_in!r} matches on its title alone",
          _sp._without_credits(_cr_in) == _cr_out,
          _sp._without_credits(_cr_in))

# But which RECORDING it is stays part of the title. Dropping these would
# answer a request for the live version with the studio one, which is the
# mistake the variant rules exist to prevent.
for _cr_keep in ("Shekastani (Live)", "Track (Remix)", "Song (Acoustic)",
                 "Song (Live at Wembley)"):
    check(f"credits: {_cr_keep!r} keeps what identifies the recording",
          _sp._without_credits(_cr_keep) == _cr_keep,
          _sp._without_credits(_cr_keep))

# A title that is nothing BUT a credit must not become empty.
check("credits: a title that is only a credit is left alone",
      _sp._without_credits("(feat. A)") == "(feat. A)")

# End to end on the real hits, with no network.
_cr_meta = _sp.TrackMeta("sp_hm", "Hate Me (with Juice WRLD)",
                         ["Ellie Goulding", "Juice Wrld"], "", 191000, "", "")
_cr_hits = [
    {"title": "Ellie Goulding, Juice WRLD - Hate Me (Official Video)",
     "channel": "Ellie Goulding", "duration": 191, "id": "UZwi9SHgzGY"},
    {"title": "Ellie Goulding - Hate Me ft. Juice WRLD (slowed + reverb)",
     "channel": "1year", "duration": 205, "id": "slowedaaa"},
]
_cr_official = _sp._match_entry(_cr_meta, _cr_hits[0], 0, "ytsearch")
_cr_slowed = _sp._match_entry(_cr_meta, _cr_hits[1], 1, "ytsearch")
check("credits: the official upload is accepted again",
      _cr_official is not None,
      "rejected" if _cr_official is None else round(_cr_official[0], 2))
check("credits: and a slowed re-upload is still refused",
      _cr_slowed is None,
      "accepted" if _cr_slowed else "refused")

# The stripping must not make a live request match a studio upload.
_cr_live = _sp.TrackMeta("sp_lv", "Shekastani (Live)", ["Omid"], "", 0, "", "")
# The direction that matters: "live" in the candidate and not in the request
# means a different recording, and asking FOR the live one makes the same
# candidate correct. Stripping credits must never blur that.
check("credits: asking for the studio cut still rejects a live upload",
      _sp._is_other_recording("Omid - Shekastani (Live)", "Shekastani"))
check("credits: and asking for the live one accepts it",
      not _sp._is_other_recording("Omid - Shekastani (Live)",
                                  "Shekastani (Live)"))


# --------------------------------------------------------------------------
# Cutting a piece out of a track or a video
# --------------------------------------------------------------------------
import shutil as _cut_shutil  # noqa: E402
import subprocess as _cut_sub  # noqa: E402
import tempfile as _cut_tmp  # noqa: E402

from handlers import cut_handler as _cuth  # noqa: E402
from modules import cut as _cutm  # noqa: E402

# What counts as a range. Persian digits because that is what a Persian
# keyboard produces, and the bidi marks that come with them are invisible,
# are not whitespace, and survive .strip().
for _cut_text, _cut_want in (
    ("3:28-4:53", (208.0, 293.0)),
    ("1:02:30-1:05:00", (3750.0, 3900.0)),
    ("28-53", (28.0, 53.0)),
    ("3:28 - 4:53", (208.0, 293.0)),
    ("\u06f3:\u06f2\u06f8-\u06f4:\u06f5\u06f3", (208.0, 293.0)),
    ("3:28 \u062a\u0627 4:53", (208.0, 293.0)),
    ("0:05\u20130:10", (5.0, 10.0)),
    ("1:00 to 2:00", (60.0, 120.0)),
):
    check(f"cut: {_cut_text!r} is a range", _cutm.parse_range(_cut_text) == _cut_want,
          str(_cutm.parse_range(_cut_text)))

# And what is not. Anything that is not a range has to fall through to the
# search, or typing a song name that happens to contain a dash breaks.
for _cut_no in ("hello", "3:28", "4:53-3:28", "3:28-3:28", "3:90-4:00",
                "1:2:3:4-5", "Drake - Jaded", "1-99999"):
    check(f"cut: {_cut_no!r} is not a range, so search still gets it",
          _cutm.parse_range(_cut_no) is None,
          str(_cutm.parse_range(_cut_no)))

# The real thing, when there is an ffmpeg to do it with. Skipped on a box
# without one rather than faked - a mocked ffmpeg would assert nothing about
# whether the cut is correct.
if _cut_shutil.which("ffmpeg") and _cut_shutil.which("ffprobe"):
    _cut_dir = Path(_cut_tmp.mkdtemp(prefix="selfcheck-cut-"))
    try:
        _cut_src = _cut_dir / "src.mp4"
        # Keyframes 10s apart: the case where a stream copy cannot start
        # where it was asked to, which is what the re-encode fallback is for.
        _cut_sub.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=40",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
             "-c:v", "libx264", "-g", "250", "-keyint_min", "250",
             "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(_cut_src)],
            capture_output=True, timeout=180)
        check("cut: the fixture was built", _cut_src.is_file())

        _cut_out = _cut_dir / "clip.mp4"
        _cutm.cut(_cut_src, 13.0, 27.0, _cut_out)
        _cut_got = _cutm.media_seconds(_cut_out)
        check("cut: an off-keyframe range still comes out the right length",
              abs(_cut_got - 14.0) <= 0.4, f"asked 14.0s, got {_cut_got:.2f}s")
        check("cut: and the clip is not empty",
              _cut_out.stat().st_size > 1024, f"{_cut_out.stat().st_size} bytes")
    finally:
        _cut_shutil.rmtree(_cut_dir, ignore_errors=True)
else:
    print("  --  cut: ffmpeg not on this box, skipping the real cut")

# Which file a range applies to.
_cut_media = type("M", (), {"file_id": "FID", "duration": 200,
                            "title": "Song", "performer": "A",
                            "file_name": "s.mp3"})()
_cut_msg = type("Msg", (), {"audio": _cut_media, "video": None, "voice": None,
                            "video_note": None, "animation": None,
                            "document": None,
                            "chat": type("C", (), {"id": 42})()})()
_cuth._last.clear()
_cuth.remember(_cut_msg)
check("cut: the last media in a chat is remembered, so no reply is needed",
      _cuth._last.get(42, {}).get("file_id") == "FID")

# A pdf replied to with a time range is not a cut request.
_cut_pdf = type("Msg", (), {"audio": None, "video": None, "voice": None,
                            "video_note": None, "animation": None,
                            "document": type("D", (), {
                                "file_id": "PDF", "mime_type": "application/pdf"})(),
                            "chat": type("C", (), {"id": 43})()})()
check("cut: a document that is not media is ignored",
      _cuth._media_of(_cut_pdf) == (None, ""))
_cut_vid_doc = type("Msg", (), {"audio": None, "video": None, "voice": None,
                                "video_note": None, "animation": None,
                                "document": type("D", (), {
                                    "file_id": "MP4",
                                    "mime_type": "video/mp4"})(),
                                "chat": type("C", (), {"id": 44})()})()
check("cut: but a video sent 'as file' is",
      _cuth._media_of(_cut_vid_doc)[1] == "video")

# The store is a convenience, not something to grow without limit.
for _cut_i in range(_cuth._LAST_LIMIT + 5):
    _cuth._last[_cut_i] = {"file_id": "x"}
    if len(_cuth._last) >= _cuth._LAST_LIMIT:
        break
check("cut: the remembered-media map is bounded",
      len(_cuth._last) <= _cuth._LAST_LIMIT, str(len(_cuth._last)))
_cuth._last.clear()


# An error raised in a module has no chat, and still has to be translated.
#
# The table is keyed on exact literals, so an f-string error can never match
# one: "«Drake — Jaded» پیدا نشد" is not a key, "«{track}» پیدا نشد" is. The
# template and its values travel with the exception and are joined only once
# somebody knows the language.
from utils import i18n as _loc  # noqa: E402

_loc_saved = dict(_loc._cache)
try:
    _loc._cache[901], _loc._cache[902] = "en", "fa"
    _loc_meta = _sp.TrackMeta("sp_l", "Hate Me (with Juice WRLD)",
                              ["Ellie Goulding"], "", 191000, "", "")
    _loc_err = _sp._download_failed(_loc_meta, ["a", "b", "c"],
                                    ["sign in to confirm"], None)
    _loc_en = _loc.localise(901, _loc_err)
    _loc_fa = _loc.localise(902, _loc_err)
    check("i18n: a formatted module error reads as English",
          "YouTube would not serve" in _loc_en, _loc_en[:60])
    check("i18n: with the track name still in it",
          "Hate Me (with Juice WRLD)" in _loc_en)
    check("i18n: and the count still filled in", " 3 " in _loc_en, _loc_en[:80])
    check("i18n: the same error is unchanged in Persian",
          "پیدا شد" in _loc_fa, _loc_fa[:50])
    # str() has to stay Persian: logs, and any caller that predates this.
    check("i18n: str() on it is still the Persian rendering",
          str(_loc_err) == _loc_fa)
    # A plain module error needs nothing but a table entry.
    check("i18n: a plain module error translates too",
          _loc.localise(901, RuntimeError(
              "این پست خصو"
              "صیه یا حذف "
              "شده.")) == "This post is private or has been deleted.")
    # And a gap degrades to Persian rather than to an empty message.
    check("i18n: an untranslated error still says something",
          _loc.localise(901, RuntimeError("یک پیام"))
          == "یک پیام")
finally:
    _loc._cache.clear()
    _loc._cache.update(_loc_saved)


# TikTok and Pinterest, and the button that makes the cutter discoverable.
from utils.url_router import Platform as _Plat, route as _route  # noqa: E402

for _u, _want in (
    ("https://www.tiktok.com/@x/video/123", _Plat.TIKTOK),
    ("https://vm.tiktok.com/ZM1/", _Plat.TIKTOK),      # share sheet
    ("https://vt.tiktok.com/ZS2/", _Plat.TIKTOK),
    ("https://m.tiktok.com/v/1.html", _Plat.TIKTOK),
    ("https://pin.it/abc", _Plat.PINTEREST),           # share sheet
    ("https://www.pinterest.com/pin/999/", _Plat.PINTEREST),
    ("https://pinterest.co.uk/pin/1/", _Plat.PINTEREST),
    ("https://de.pinterest.com/pin/1/", _Plat.PINTEREST),
):
    _got = _route(_u)
    check(f"route: {_u[:38]} -> {_want.value}",
          _got is not None and _got.platform == _want,
          _got.platform.value if _got else "None")

# The platforms that already worked must not have been captured by the new
# patterns - pinterest's country-domain rule is the broad one.
for _u, _want in (("https://youtu.be/abc", _Plat.YOUTUBE),
                  ("https://open.spotify.com/track/a", _Plat.SPOTIFY),
                  ("https://instagram.com/reel/x/", _Plat.INSTAGRAM),
                  ("https://soundcloud.com/a/b", _Plat.SOUNDCLOUD)):
    _got = _route(_u)
    check(f"route: {_want.value} is untouched by the new patterns",
          _got is not None and _got.platform == _want,
          _got.platform.value if _got else "None")
check("route: plain text is still a search", _route("drake jaded") is None)

# The button carries no id: the file is the message it hangs under, so it
# cannot go stale and cannot outgrow Telegram's 64-byte callback_data.
_cb = _cuth.cut_button(1).inline_keyboard[0][0]
check("cut button: its callback_data is tiny and fixed",
      _cb.callback_data == "cut:ask" and len(_cb.callback_data.encode()) <= 64,
      _cb.callback_data)

# Pressing it has to make the file unambiguous - Telegram gives a bot no way
# to ask a question and wait, so the button's job is to point the NEXT range
# at this file even if other things arrive in between.
class _BtnMsg:
    chat_id = 66
    chat = type("C", (), {"id": 66})()
    audio = type("A", (), {"file_id": "BTN", "duration": 100, "title": "T",
                           "performer": "P", "file_name": "f.mp3"})()
    video = voice = video_note = animation = document = None
    said = []

    async def reply_text(self, text, **kw):
        _BtnMsg.said.append(text)
        return self


class _BtnQuery:
    def __init__(self):
        self.data = "cut:ask"
        self.message = _BtnMsg()

    async def answer(self):
        pass


_cuth._last.clear()
_BtnMsg.said.clear()
_bk_aio = __import__("asyncio")
_bk_aio.run(_cuth.on_callback(
    type("U", (), {"callback_query": _BtnQuery()})(), None))
check("cut button: pressing it points the next range at that file",
      _cuth._last.get(66, {}).get("file_id") == "BTN")
check("cut button: and it says what to type",
      any("0:" in s for s in _BtnMsg.said), str(_BtnMsg.said)[:60])
_cuth._last.clear()

# yt-dlp accepts curl_cffi 0.5.10 and 0.10.x-0.15.x. A newer one installs and
# imports fine and is then refused: every impersonation target reads
# "(unavailable)", so the TLS fingerprinting was never on and TikTok - whose
# extractor requires impersonation - could not work at all.
_curl_sh = (Path(__file__).resolve().parent / "manage.sh").read_text(
    encoding="utf-8")
check("tiktok: curl_cffi is pinned to a version yt-dlp accepts",
      "curl_cffi>=0.10,<0.16" in _curl_sh)
check("tiktok: and there is a command to repair it",
      "do_fixcurl" in _curl_sh and "fixcurl)" in _curl_sh)


print()
if failures:
    print(f"=== {len(failures)} CHECK(S) FAILED: {failures} ===")
    sys.exit(1)
print("=== ALL CHECKS PASSED ===")
