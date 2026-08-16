# Session prompt — Instagram Direct, continuing

Paste everything below into a fresh session.

---

You are continuing work on a deployed Telegram downloader bot. Reply to the
user in **Persian (Farsi)**. Code, comments and commit messages stay in
English.

## The project

**Repo:** `github.com/moeinimy/telegram_downloader_bot`
**Local:** `D:\claude projects\telegram_downloader_bot`
**Server:** Ubuntu VPS `213.182.213.112`, deployed by `git push` then
`botctl update`. `deploy/manage.sh` is the installer and the `botctl` CLI.
**Never** ask the user to paste file contents — ship via git.

Verify every change with `python deploy/selfcheck.py` (179 checks, all
passing) plus an import sweep of all 41 modules. That script is the
project's regression suite; add to it rather than testing by hand.

## The feature

Users pair their Instagram account to their Telegram chat with a one-time
token, then anything they share to the bot's Instagram DMs comes back to them
in Telegram, downloaded. `/igdirect` drives the pairing.

Four interchangeable inbox sources behind one `Source` interface in
`modules/ig_direct.py`, preferred in this order:

| source | file | state |
|---|---|---|
| `webhook` | `web/webhook.py` | built, unusable — see below |
| `mqtt` | `modules/ig_realtime.py` | **just built, needs live testing** |
| `web` | `modules/ig_web.py` | works |
| `poll` | `modules/ig_private.py` | dead for this account, kept as last resort |

Item parsing is shared in `modules/ig_items.py` and is done **by shape, not by
key name** — Instagram has renamed this payload three times during
development and each rename silently broke one kind of share.

## Where things stand

**The official Meta path is closed.** `instagram_business_manage_messages`
still requires business verification in 2026, and the user cannot verify a
Meta developer account (no SMS, virtual numbers refused). Confirmed by
research; do not suggest it as a near-term fix.

**The unofficial path keeps getting the account checkpointed** — three times
in about a week. Lowering the poll rate from 5,760 requests/day to 396 did
**not** stop it, which is the evidence that request rate was never the thing
being noticed. The real Instagram app does not poll; it holds an MQTT
connection. That is why `modules/ig_realtime.py` exists.

**Realtime has connected once, on the sessionid cookie.** Three bugs were
found and fixed immediately after (idle reads treated as disconnects; the
teardown calling `accounts/logout/` and destroying the session; a health check
declaring it dead one second after start). The proxy scheme bug was fixed a
fourth time and moved to `utils/proxies.py`.

**It has not yet been confirmed end to end.** Nobody has watched a reel go
Instagram DM → Telegram over MQTT.

## What to do first

Ask the user to run:

```bash
botctl update && botctl restart && botctl logs
```

Expect `ig mqtt: realtime connected - polling is no longer needed`, then
silence — no `connection lost`, no `logout`, no per-request logs.

Then have them share a reel to the bot's Instagram account from a **second**
account that **follows** the page, and check:

- the reel arrives in Telegram, near-instantly
- `/srcstatus` shows `mqtt` healthy, `web` on standby, and the request rate
  falling toward zero

That last number is the real measure: if the hourly rate goes to zero, the
account stops producing the polling pattern that caused every checkpoint.

If realtime cannot hold, the supervisor falls back to the `web` poller
automatically and nothing is lost — say so rather than treating it as an
outage.

## Diagnostics that already exist — use them before theorising

    botctl igtest2     one-shot: config, network, login, inbox read, item ages
    botctl igwatch     live pipeline, stage by stage, while you send a DM
    botctl shazamtest  what Shazam actually returns to this server
    botctl deps        package versions, conflicts, disk
    botctl proxy       set/test/disable the proxy for Instagram and Shazam
    /srcstatus         sources, request rate with a verdict, disk, channel lock

## Hard-won facts — do not re-derive these

- A browser `sessionid` belongs to the **web** api. Giving it to the mobile
  api (`i.instagram.com`, instagrapi) returns 403 `login_required` forever, no
  matter the device fingerprint, proxy or cookie freshness.
- The web api needs three cookies (`sessionid`, `csrftoken`, `ds_user_id`),
  the header `X-IG-App-ID: 936619743392459`, and `X-IG-WWW-Claim` echoed back
  from the previous response.
- Instagram **rotates** `sessionid` mid-session. One long-lived client with a
  persisted cookie jar is required; a fresh client per request throws the
  rotation away and gets a ~608KB login page a few calls later.
- A newly pasted `IG_DM_SESSIONID` must beat the stored jar, or a fix appears
  to do nothing.
- `direct_v2/pending_inbox/` is mobile-only; `inbox/?folder=1` is the web
  equivalent for message requests.
- DM timestamps must be parsed **by magnitude**, not assumed to be
  microseconds — the wrong unit puts every message in 1970 and the poll loop
  silently delivers nothing while reading the inbox perfectly.
- A `checkpoint_required` sometimes lifts on its own within hours. Back off
  (30m → 1h → 2h) and keep checking; do **not** stop permanently.
- `socks5h://` is rejected by aiohttp-socks, httpx and aiograpi. Always go
  through `utils.proxies.normalize()`.

## Also open

- **Music recognition fallbacks are misconfigured.** `/recstatus` shows
  `acoustid HTTP 400` and `audd authorization failed`. AcoustID's 400 is
  usually the wrong key — the **Application** API key is required, not the
  user one. Until one works, anything Shazam misses gets no answer.
- **Shazam works** through the WARP proxy (`botctl shazamtest` returns
  `200 application/json`). Recognition timing is instrumented; the last
  measured run was `identify 1.7s`.
- `IG_PUBLIC_URL not set` in `/srcstatus` is expected and harmless — it only
  matters for the Meta webhook path, which is unused.

## House style

- Comments explain *why*, never *what*. Match the density in the files.
- Commit messages: what broke, the root cause, and real before/after output.
  No adjectives.
- End commits with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Measure before changing. Several wrong diagnoses in this feature came from
  reasoning about the cause instead of reading a log; every one was settled by
  an actual number.
- Do not break the pasted-link flow, the sponsor gate, the admin panel or the
  music matcher.
