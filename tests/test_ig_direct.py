"""
Section 7 of the brief, the parts that can be proven without a real account.

Runs the ACTUAL aiohttp listener over loopback and posts real requests at it,
rather than calling the handler functions directly - the signature check and
the 403 path are the whole point, and a direct call would skip the middleware
and the routing that a real Meta delivery goes through.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

WORK = tempfile.mkdtemp(prefix="igdirect-test-")
SECRET = "test-app-secret-0123456789"
PORT = 18099
PATH = "/ig/webhook"

os.environ.update(
    TELEGRAM_BOT_TOKEN="123456:test-token-not-real",
    DOWNLOAD_DIR=WORK,
    IG_APP_SECRET=SECRET,
    IG_ACCESS_TOKEN="test-access-token",
    IG_VERIFY_TOKEN="test-verify-token",
    IG_WEBHOOK_HOST="127.0.0.1",
    IG_WEBHOOK_PORT=str(PORT),
    IG_WEBHOOK_PATH=PATH,
    IG_DIRECT_SOURCES="webhook",
    ADMIN_IDS="",
    REQUIRED_CHANNEL="",
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from modules import ig_direct, ig_pairing  # noqa: E402
from modules.instagram import media_id_to_shortcode  # noqa: E402
from web import webhook  # noqa: E402

BASE = f"http://127.0.0.1:{PORT}{PATH}"

passed, failed = 0, 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# The real media id behind shortcode DZfwtaiob79 (the control reel the project
# already uses in modules/instagram.diagnose).
REEL_MEDIA_ID = "3918064427942919933"
REEL_SHORTCODE = "DZfwtaiob79"


def reel_payload(mid: str, igsid: str = "17841400000000001", media_id: str = REEL_MEDIA_ID) -> bytes:
    return json.dumps({
        "object": "instagram",
        "entry": [{
            "id": "17841499999999999",
            "time": 1770000000,
            "messaging": [{
                "sender": {"id": igsid},
                "recipient": {"id": "17841499999999999"},
                "timestamp": 1770000000000,
                "message": {
                    "mid": mid,
                    "attachments": [{
                        "type": "ig_reel",
                        "payload": {
                            "reel_video_id": media_id,
                            "title": "a reel",
                            "url": "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1&signature=x",
                        },
                    }],
                },
            }],
        }],
    }).encode()


def text_payload(mid: str, text: str, igsid: str = "17841400000000002") -> bytes:
    return json.dumps({
        "object": "instagram",
        "entry": [{
            "id": "17841499999999999",
            "messaging": [{
                "sender": {"id": igsid},
                "timestamp": 1770000000000,
                "message": {"mid": mid, "text": text},
            }],
        }],
    }).encode()


async def main() -> None:
    seen: list[ig_direct.DirectMessage] = []

    async def handler(dm: ig_direct.DirectMessage) -> None:
        seen.append(dm)

    ig_direct._on_message = handler
    await webhook.start(ig_direct._dispatch)

    async with httpx.AsyncClient(timeout=10) as client:
        # ---- 1. signature verification ----
        print("\n1) signature verification")
        body = reel_payload("mid-signature-test")

        r = await client.post(BASE, content=body, headers={"X-Hub-Signature-256": sign(body, "WRONG-SECRET")})
        check("wrong secret is rejected", r.status_code == 403, f"got {r.status_code}")

        r = await client.post(BASE, content=body)
        check("missing signature is rejected", r.status_code == 403, f"got {r.status_code}")

        r = await client.post(BASE, content=body, headers={"X-Hub-Signature-256": sign(body, SECRET).replace("sha256", "sha1")})
        check("wrong algorithm prefix is rejected", r.status_code == 403, f"got {r.status_code}")

        r = await client.post(BASE, content=body, headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        check("truncated digest is rejected", r.status_code == 403, f"got {r.status_code}")

        tampered = body.replace(b"a reel", b"b reel")
        r = await client.post(BASE, content=tampered, headers={"X-Hub-Signature-256": sign(body, SECRET)})
        check("tampered body is rejected", r.status_code == 403, f"got {r.status_code}")

        check("nothing was dispatched by a rejected request", len(seen) == 0, f"dispatched {len(seen)}")

        r = await client.post(BASE, content=body, headers={"X-Hub-Signature-256": sign(body, SECRET)})
        check("correct signature is accepted", r.status_code == 200, f"got {r.status_code}")

        # ---- 2. verification handshake ----
        print("\n2) subscription handshake")
        r = await client.get(BASE, params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "42424242"})
        check("correct verify token echoes the challenge", r.status_code == 200 and r.text == "42424242", f"got {r.status_code} {r.text!r}")

        r = await client.get(BASE, params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42424242"})
        check("wrong verify token is rejected", r.status_code == 403, f"got {r.status_code}")

        # ---- 3. duplicate mid ----
        print("\n3) duplicate delivery")
        await asyncio.sleep(0.3)
        seen.clear()

        # A different sender, because section 1 already delivered this reel and
        # the content key would (correctly) call the replay a duplicate of it.
        dupe = reel_payload("mid-replay-test", igsid="17841400000000777")
        headers = {"X-Hub-Signature-256": sign(dupe, SECRET)}
        for _ in range(3):
            r = await client.post(BASE, content=dupe, headers=headers)
            assert r.status_code == 200
        await asyncio.sleep(0.5)
        check("the same mid delivered 3x is handled once", len(seen) == 1, f"handled {len(seen)}x")

        # ---- 4. cross-source duplicate ----
        print("\n4) cross-source duplicate (the failover case)")
        seen.clear()
        now = time.time()
        common = dict(igsid="17841400000000009", permalink="https://www.instagram.com/reel/DZfwtaiob79/", timestamp=now)
        await ig_direct._dispatch(ig_direct.DirectMessage(mid="meta-abc", source="webhook", **common))
        await ig_direct._dispatch(ig_direct.DirectMessage(mid="instagrapi-999", source="poll", **common))
        await asyncio.sleep(0.3)
        check("same share seen by both sources is handled once", len(seen) == 1, f"handled {len(seen)}x")

        seen.clear()
        await ig_direct._dispatch(ig_direct.DirectMessage(mid="meta-def", source="webhook", igsid=common["igsid"], permalink=common["permalink"], timestamp=now + 3600))
        await asyncio.sleep(0.3)
        check("the same reel an hour later is a new request", len(seen) == 1, f"handled {len(seen)}x")

        # ---- 5. payload parsing ----
        print("\n5) payload parsing")
        parsed = webhook.parse_entries(json.loads(reel_payload("mid-parse")))
        check("one reel attachment yields one message", len(parsed) == 1, f"got {len(parsed)}")
        check("shortcode is derived from the reel media id", parsed[0].shortcode() == REEL_SHORTCODE, f"got {parsed[0].shortcode()!r}")
        check("identity is namespaced for the webhook", parsed[0].identity().startswith("ig:"), parsed[0].identity())

        echo = json.loads(reel_payload("mid-echo"))
        echo["entry"][0]["messaging"][0]["message"]["is_echo"] = True
        check("our own echoed message is ignored", webhook.parse_entries(echo) == [])

        receipt = {"object": "instagram", "entry": [{"messaging": [{"sender": {"id": "1"}, "read": {"mid": "x"}}]}]}
        check("a read receipt is ignored", webhook.parse_entries(receipt) == [])

        parsed_text = webhook.parse_entries(json.loads(text_payload("mid-text", "IG-7QK4M2")))
        check("a plain text DM yields one message", len(parsed_text) == 1, f"got {len(parsed_text)}")
        check("its text survives", parsed_text[0].text == "IG-7QK4M2")

        # ---- 6. shortcode round trip ----
        print("\n6) shortcode <-> media id")
        from modules.instagram import _shortcode_to_media_id

        for code in ("DZfwtaiob79", "Bt4k7fjnRRl", "CxY-_1234ab"):
            back = media_id_to_shortcode(str(_shortcode_to_media_id(code)))
            check(f"round trip {code}", back == code, f"got {back!r}")
        check("id with an owner suffix still resolves", media_id_to_shortcode(f"{REEL_MEDIA_ID}_17841400000000001") == REEL_SHORTCODE)
        check("junk id yields empty, not a crash", media_id_to_shortcode("not-a-number") == "")

        # ---- 7. pairing ----
        print("\n7) pairing")
        token = ig_pairing.issue(555)
        check("token has the expected shape", ig_pairing.looks_like_token(token), token)
        check("no ambiguous characters", not (set("01IOL") & set(token[3:])), token)
        check("an ordinary sentence is not mistaken for a token", not ig_pairing.looks_like_token("hey what's up"))

        check("lowercase and spaces still redeem", ig_pairing.redeem(token.lower().replace("IG-", "ig- "), "ig:900") == 555)
        check("the token is single use", ig_pairing.redeem(token, "ig:901") is None)
        check("the sender resolves to the chat", ig_pairing.chat_for("ig:900") == 555)

        second = ig_pairing.issue(555)
        check("a second issue invalidates the first", ig_pairing.redeem(token, "ig:902") is None)
        check("pairing the poll namespace keeps the webhook one", ig_pairing.redeem(second, "pk:12345") == 555)
        check("both identities now resolve", ig_pairing.chat_for("ig:900") == 555 and ig_pairing.chat_for("pk:12345") == 555)
        check("both are listed for the chat", sorted(ig_pairing.linked_ids(555)) == ["ig:900", "pk:12345"], ig_pairing.linked_ids(555))

        third = ig_pairing.issue(555)
        check("re-pairing in a namespace replaces it", ig_pairing.redeem(third, "ig:777") == 555)
        check("the replaced webhook identity is gone", ig_pairing.chat_for("ig:900") is None)
        check("the poll identity is untouched", ig_pairing.chat_for("pk:12345") == 555)

        check("unlink removes everything for the chat", ig_pairing.unlink(555) and ig_pairing.linked_ids(555) == [])

        expired = ig_pairing.issue(556)
        store = ig_pairing._load()
        store["pending"][expired]["expires"] = time.time() - 1
        check("an expired token does not redeem", ig_pairing.redeem(expired, "ig:903") is None)

        # ---- 8. persistence ----
        print("\n8) persistence across a restart")
        ig_pairing.redeem(ig_pairing.issue(777), "ig:808")
        ig_pairing._data = None  # force a re-read from disk
        check("links survive a reload", ig_pairing.chat_for("ig:808") == 777)

        ig_direct._seen = None
        check("seen ids survive a reload", ig_direct.already_seen(ig_direct.DirectMessage(igsid="17841400000000009", permalink=common["permalink"], timestamp=now, mid="meta-abc", source="webhook")))

    await webhook.stop()

    print(f"\n{'=' * 46}\n{passed} passed, {failed} failed\n{'=' * 46}")
    sys.exit(1 if failed else 0)


try:
    asyncio.run(main())
finally:
    shutil.rmtree(WORK, ignore_errors=True)
