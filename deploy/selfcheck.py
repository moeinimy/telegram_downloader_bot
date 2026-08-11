"""Verification for the Instagram Direct feature (brief section 7)."""

import ast
import builtins
import hashlib
import hmac
import json
import os
import sys
import tempfile
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


print()
if failures:
    print(f"=== {len(failures)} CHECK(S) FAILED: {failures} ===")
    sys.exit(1)
print("=== ALL CHECKS PASSED ===")
