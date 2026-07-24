# Telegram Multi-Platform Downloader Bot

Async Telegram bot that downloads media from **YouTube**, **Instagram**, and **Spotify**.

## Folder structure

```
telegram_downloader_bot/
├── main.py                 # entry point, builds the Application
├── config.py               # env-driven settings
├── requirements.txt
├── .env.example
│
├── handlers/               # Telegram-side logic (no business logic here)
│   ├── start.py            # /start /help
│   ├── router.py           # text → URL detection → dispatch
│   ├── youtube_handler.py
│   ├── instagram_handler.py
│   └── spotify_handler.py
│
├── modules/                # Pure downloader/business logic, no Telegram types
│   ├── youtube.py          # yt-dlp wrapper
│   ├── instagram.py        # instaloader wrapper
│   └── spotify.py          # spotipy meta + spotdl/yt-dlp audio
│
├── utils/
│   ├── url_router.py       # regex-based platform/kind detection
│   ├── progress.py         # throttled "edit message" progress reporter
│   └── helpers.py          # filename/size/duration utilities
│
└── downloads/              # temp working dir (auto-created)
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env             # then fill in tokens

# Required system dependency:
# ffmpeg must be on PATH (used by yt-dlp/spotdl for audio extraction)
```

Fill `.env`:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — https://developer.spotify.com/dashboard
- `INSTAGRAM_USERNAME` / `INSTAGRAM_PASSWORD` — *optional*, only needed for stories

## Run

```bash
python main.py
```

## How requests are routed

```
text message ─→ handlers/router.py:handle_text
                    │
                    ├── utils/url_router.route(text)
                    │     ├── spotify.com/{track,album,playlist,artist}/…  → spotify_handler
                    │     ├── youtube.com / youtu.be / shorts                → youtube_handler
                    │     └── instagram.com/{p,reel,stories,<user>}/…       → instagram_handler
                    │
                    └── no URL → spotify_handler.handle_search (free-text)
```

Inline buttons are namespaced by prefix:
`yt:*`, `ig:*`, `sp:*` — each module owns its callbacks.

## Feature coverage

| Platform | Capability |
|---|---|
| YouTube | thumbnail + title preview, quality picker (360/480/720/1080/best), audio-only MP3 with embedded artwork |
| Instagram | single post, reel, carousel (album send), stories (login required), profile picture |
| Spotify | track / album / playlist / artist-top-10, free-text search, per-track buttons, bulk download, fallback to YouTube search when Spotify source missing |

## Limits

- Telegram bot upload limit is **50 MB** for the public Bot API.
  For larger files (e.g. 1080p videos) run your own [local Bot API server](https://github.com/tdlib/telegram-bot-api) and bump `MAX_UPLOAD_MB`.
- Instagram stories require a valid login session and may trigger rate-limits.
- Spotify only exposes metadata via API; actual audio is sourced via `spotdl`/`yt-dlp`.
