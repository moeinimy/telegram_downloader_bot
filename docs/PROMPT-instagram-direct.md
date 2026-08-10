# Session prompt — Instagram Direct → Telegram bridge

Paste everything below into a fresh session.

---

You are continuing work on an existing, deployed Telegram downloader bot. Reply
to the user in **Persian (Farsi)** — they are a Persian speaker. Code, comments
and commit messages stay in English.

## 1. What already exists (do not re-derive this)

**Repo:** `github.com/moeinimy/telegram_downloader_bot` (public)
**Local:** `D:\claude projects\telegram_downloader_bot`
**Server:** Ubuntu 22.04 VPS at `213.182.213.112`, deployed by `git push` then
`botctl update` on the box. `deploy/manage.sh` is the installer/menu and also
provides the non-interactive `botctl <cmd>` (`update`, `restart`, `logs`,
`clearcache`, …). **Never** ask the user to paste file contents — ship via git.

**Stack:** Python 3.13, `python-telegram-bot` 21.6 (async), `yt-dlp`, `httpx`,
`mutagen`. Runs under systemd. A local Bot API container
(`aiogram/telegram-bot-api`) lifts the upload cap to 2GB — it is configured with
`TELEGRAM_LOCAL=1` as an **env var**, not a CMD arg.

**Layout that matters here:**

| Path | What it does |
|---|---|
| `main.py` | builds the `Application`, registers every handler |
| `handlers/router.py` | routes incoming URLs to the right platform handler |
| `handlers/instagram_handler.py` | the existing IG download flow |
| `handlers/gate.py` | sponsor-channel membership gate (`TypeHandler`, group=-1) |
| `handlers/admin.py` | `/admin` panel, stats, broadcast, `/igtest`, `/srcstatus` |
| `handlers/start.py` | `/start`, language picker |
| `modules/instagram.py` | **cookie-free** IG fetch: 4 routes with adaptive ordering |
| `modules/stats.py` | user/download bookkeeping |
| `utils/i18n.py` | translation map keyed on the **Persian** source string |
| `utils/limits.py` | global + per-user semaphores, thread pools, disk sweeper |
| `utils/file_cache.py` | persistent Telegram `file_id` cache |
| `config.py` | `settings`, loaded from `.env`; `.env.example` documents every key |

`modules/instagram.py` already downloads a reel/post from a shortcode without
any login, and `handlers/instagram_handler.py` already turns that into a
Telegram upload with caption. **The new feature must reuse both.** Its only new
responsibility is *learning what to download from an Instagram DM instead of
from a message the user typed.*

## 2. The feature

Today a user pastes an Instagram link into Telegram. We want a second, hands-off
route:

1. In the Telegram bot the user opens a new menu item and enables
   **"Instagram Direct"**.
2. The bot shows them a short one-time **pairing token** (e.g. `IG-7QK4M2`).
3. The user sends that token as a DM to **our** Instagram account.
4. The backend receives the DM, reads the token, and permanently links that
   Instagram user (their IGSID) to that Telegram chat id.
5. From then on, **anything that user shares to our Instagram DM** — reel, post,
   story, video — comes back to them **in Telegram**, already downloaded, with
   the caption and the original link.

The reference implementation the user showed is `@Regrambot`, which replies in
Telegram with the media plus buttons: view on Instagram, refresh post info,
download audio only, get all qualities, get the direct link, get the caption.
Treat that button set as the target once the core loop works — **build the core
loop first**.

## 3. The architectural decision — resolve this FIRST, with the user

There are exactly two ways to read our own Instagram inbox. **Do not start
coding until the user has chosen.** Present both honestly, with a
recommendation.

### Path A — Instagram Platform API (official). Recommended.

Verified from Meta's current docs:

- Works with **Instagram API with Instagram Login** — a Facebook Page is **not**
  required (this changed; older tutorials say otherwise).
- Our account must be a **Professional** account (Business or Creator).
- Scope needed: **`instagram_business_manage_messages`** (the old
  `business_manage_messages` was deprecated 2025-01-27).
- Auth: Business Login → short-lived token (1 hour) → exchange for a
  **long-lived token (60 days)** → refresh before expiry (token must be ≥24h
  old, needs `instagram_business_basic`). **A token unused for 60 days dies
  permanently and cannot be refreshed** — so the refresh job is not optional.
- Delivery is by **webhook**, field `messages`. Other fields available:
  `message_echoes`, `message_reactions`, `messaging_postbacks`,
  `messaging_seen`, `messaging_referral`, `messaging_handover`.
- A shared reel arrives as an attachment of type **`ig_reel`** carrying the
  reel's `url`, `title` and video id. Shared posts arrive as `share`. Plain text
  (our pairing token) arrives as `message.text`.
- The sender is identified by an **IGSID** — an Instagram-scoped id, stable for
  our account. That IGSID is what we key the pairing on.

**MUST VERIFY during setup, do not assume:** whether Standard Access is enough.
Meta distinguishes Standard Access (accounts you own and add to the app) from
Advanced Access (accounts you don't). We own the receiving account, but Meta has
historically gated *messaging with the general public* behind App Review. Test
this early with a second account that has **no role on the app** — if it fails,
App Review is on the critical path and the user must know before anything else
is built.

### Path B — unofficial private API (`instagrapi` or similar)

Log into the account like a phone would and poll the inbox. This is what most
Telegram "Regram" bots do. Honest trade-offs:

- Works immediately, no App Review, no webhook, no public HTTPS endpoint.
- Violates Instagram's Terms of Use. The account can be disabled without appeal,
  and datacenter IPs are flagged aggressively.
- Requires storing the account's session; a challenge/2FA prompt breaks it
  silently and needs manual re-login.
- Breaks whenever Instagram changes its private endpoints.

**Recommend Path A.** It is the only version that survives. Offer Path B only if
the user explicitly accepts the ban risk, and if they do, isolate it behind the
same internal interface (§5) so it can be swapped out.

## 4. What the user must do themselves (tell them this up front)

You cannot do any of these for them, and Claude must never enter their
passwords, tokens, or API keys anywhere. The user pastes secrets into `.env` on
their own server; the code reads them from there.

1. **Create the Instagram account** for the bot and switch it to a
   **Professional** account (Settings → Account type → Switch to professional).
2. In Instagram app settings, make sure **message controls allow connected
   tools** to access messages (Settings → Messages → "Connected tools" /
   "Allow access to messages"). Without this the webhook stays silent.
3. **Create a Meta app** at `developers.facebook.com` → add the **Instagram**
   product → Business Login. Note the **App ID** and **App Secret**.
4. **A domain name pointing at the VPS with a valid TLS certificate.** Meta only
   delivers webhooks to public HTTPS with a trusted cert — a bare IP will not
   work. Cheapest routes: a cheap domain + Caddy/Nginx + Let's Encrypt, or a
   Cloudflare Tunnel. **Ask them which they want; this is the one piece of
   infrastructure the feature cannot exist without.**
5. Run the Business Login flow once to authorise the app against their own
   Instagram account, and put the resulting long-lived token in `.env`.
6. Decide the **verify token** string for the webhook handshake.
7. If Standard Access turns out to be insufficient (§3), submit **App Review**
   for `instagram_business_manage_messages` — screencast, use-case description,
   privacy policy URL. This takes days, sometimes weeks.

## 5. What to build

Design so that Path A and Path B are interchangeable behind one interface.

### `modules/ig_direct.py` — the inbox source
```
async def start(on_message: Callable[[DirectMessage], Awaitable[None]]) -> None
@dataclass DirectMessage:
    igsid: str            # who sent it
    text: str             # for the pairing token
    media_url: str        # ig_reel / share attachment url, when present
    permalink: str        # the instagram.com link, when derivable
    raw: dict             # keep the original payload for debugging
```
Path A implements this over the webhook; Path B over a poll loop. Nothing else
in the bot may import Meta-specific types.

### `modules/ig_pairing.py` — the link between the two identities
- `issue(chat_id) -> token` — short, unambiguous alphabet (no `0/O`, `1/I/l`),
  **single-use**, **expires in ~15 minutes**.
- `redeem(token, igsid) -> chat_id | None`
- `unlink(chat_id)` and `linked_igsid(chat_id)`.
- Persist to disk atomically, in the style of `utils/file_cache.py` (temp file +
  `replace`, so a crash mid-write cannot corrupt it). A JSON file is fine; the
  project has no database and does not need one.

### `web/webhook.py` — the HTTPS endpoint (Path A only)
- `GET` → Meta's verification handshake (`hub.mode`, `hub.verify_token`,
  `hub.challenge`).
- `POST` → **verify `X-Hub-Signature-256` (HMAC-SHA256 with the app secret)
  before parsing anything.** An unsigned or mis-signed request is discarded.
  This endpoint is on the public internet; treat every byte as hostile.
- Respond **200 immediately**, then hand off to the bot's event loop. Meta
  retries on timeout and will duplicate work otherwise.
- **De-duplicate on message id (`mid`)** — retries are normal, not exceptional.
- Prefer running it inside the existing process (aiohttp/FastAPI on a local
  port, reverse-proxied) over a second service. Fewer moving parts to deploy.

### `handlers/ig_direct_handler.py` — the Telegram side
- Menu entry + `/igdirect` to enable, show the token, show current link status,
  and unlink.
- On a paired DM: run the **existing** `modules/instagram.py` pipeline, then the
  **existing** upload path, so captions, quality choices and the file-id cache
  all work exactly as they do today. Do not fork that logic.
- Every user-visible string goes through `utils/i18n.py`, keyed on the Persian
  text, with an English translation added in the same commit.
- Downloads go through `utils.limits` like every other download. A user who
  shares thirty reels at once must not be able to monopolise the server.

## 6. Requirements that are easy to skip and will hurt later

- **Media URLs from the webhook are short-lived.** Fetch immediately; do not
  queue the URL for later.
- **Unpaired sender** → do not silently drop. Reply in the DM explaining how to
  pair.
- **The 24-hour messaging window**: Instagram only allows a reply inside 24h of
  the user's last message. Replies to Instagram are for pairing feedback only —
  the media always goes to Telegram, so this must never block delivery.
- **The user blocks the account or deletes the chat** → the pairing must expire
  cleanly rather than wedge.
- **Token refresh** — a scheduled job, plus a loud admin alert if it ever fails.
  Sixty days of silence ends with a permanently dead token.
- **Privacy.** This feature makes the bot receive a user's Instagram identity.
  Store the IGSID and nothing else. Say so in the enable screen, and make
  unlinking one tap.
- **Rate limits.** Meta's messaging limits are per-account, not per-user.
- Extend `/srcstatus` (already in `handlers/admin.py`) with the webhook's health:
  last received event, token expiry date, number of active pairings.

## 7. How to verify — do not report success without this

The user has been burned repeatedly by changes that looked right and were not.
This project's standard is: **measure, do not assume.**

1. `python -c "import main"` plus an import sweep of every module.
2. The AST undefined-name check used throughout this project (it has caught real
   `NameError`s that reached production).
3. i18n completeness: every new Persian string has an English entry.
4. **Signature verification test with a deliberately wrong secret** — it must
   reject.
5. **Replay the same `mid` twice** — it must upload once.
6. A real end-to-end run: pair a second Instagram account, share a reel, confirm
   it lands in Telegram with caption and link.
7. Confirm an **unpaired** account gets the pairing instructions and nothing else.

## 8. House style

- Comments explain *why*, never *what*. Match the density already in the files.
- Commit messages: what broke, what the root cause was, what the evidence is.
  Show real before/after output, not adjectives.
- End commits with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Do not break the existing link-paste flow, the sponsor gate, the admin panel,
  or the music matcher. They took a long time to get right.

## 9. Start here

Before writing any code:

1. Read `modules/instagram.py`, `handlers/instagram_handler.py`, `main.py`,
   `utils/i18n.py` and `deploy/manage.sh` so the new code matches the project.
2. Put §3's decision to the user and get an answer.
3. Confirm they have a domain + TLS, or agree on Cloudflare Tunnel.
4. Then write a plan, and only then build it.
