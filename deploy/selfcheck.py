"""Verification for the Instagram Direct feature (brief section 7)."""

import ast
import builtins
import hashlib
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
