"""
What does Shazam actually send back to THIS server?

"FailedDecodeJson" only says the body was not json. It does not say whether
that body was a block page, a captcha, a 403, a redirect to a consent wall or
an empty response - and those want different answers. This traces the HTTP
layer underneath shazamio and prints the status, the content type and the
first part of the body.

    botctl shazamtest [path/to/audio-or-video]

With no path it uses the newest media under downloads/, and falls back to a
generated tone. A tone will never match a song - that is fine, the question
here is what the endpoint replies, not what it recognises.

Tracing is done by wrapping aiohttp rather than anything shazamio-specific,
so it keeps working across shazamio versions. Reading the body inside the
wrapper is safe: aiohttp caches it on the response, so the library's own
.json() call afterwards sees exactly what we printed.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_MEDIA_EXTS = {".mp3", ".m4a", ".mp4", ".wav", ".ogg", ".mov", ".webm"}


def pick_audio() -> Path | None:
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1])
        if candidate.is_file():
            return candidate
        print(f"[X] {candidate} پیدا نشد")
        return None

    from config import settings

    files = [
        p for p in settings.download_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _MEDIA_EXTS and p.stat().st_size > 50_000
    ]
    if files:
        newest = max(files, key=lambda p: p.stat().st_mtime)
        print(f"[*] استفاده از آخرین فایل دانلودشده: {newest.name}")
        return newest

    tone = settings.download_dir / "shazamtest_tone.mp3"
    print("[!] فایل مدیایی پیدا نشد - یه صدای تستی می‌سازم")
    print("    (هیچ آهنگی باهاش match نمی‌شه؛ سوال اینه که سرور چی جواب می‌ده)")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=12", "-ar", "44100", "-ac", "1", str(tone)],
        check=False,
    )
    return tone if tone.exists() else None


def install_trace() -> list[dict]:
    """Wrap aiohttp so every request Shazam makes is visible."""
    import aiohttp

    seen: list[dict] = []
    original = aiohttp.ClientSession._request

    async def traced(self, method, url, **kwargs):
        response = await original(self, method, url, **kwargs)
        try:
            # Caches onto the response, so the library's own read still works.
            body = await response.read()
        except Exception as e:
            body = f"<could not read body: {e}>".encode()
        seen.append({
            "method": method,
            "url": str(url).split("?")[0],
            "status": response.status,
            "type": response.headers.get("Content-Type", "?"),
            "server": response.headers.get("Server", "?"),
            "length": len(body),
            "head": body[:400],
        })
        return response

    aiohttp.ClientSession._request = traced
    return seen


def verdict(calls: list[dict]) -> None:
    print()
    print("=" * 60)
    if not calls:
        print("[X] هیچ درخواست HTTPی زده نشد - یعنی خطا قبل از شبکه‌ست.")
        print("    یعنی باگ سمت ماست یا فایل صوتی خونده نشده، نه بلاک شدن.")
        return

    api = [c for c in calls if "shazam" in c["url"]] or calls
    worst = max(api, key=lambda c: c["status"])
    body = worst["head"].decode("utf-8", "replace").strip()
    ctype = worst["type"].lower()

    print(f"[*] آخرین پاسخ شزم: HTTP {worst['status']}  ({worst['type']})")
    print()

    if worst["status"] in (403, 401):
        print("[X] رد شده. IP این سرور برای شزم بلاک شده.")
        print("    راه‌حل: SHAZAM_PROXY. تکرار درخواست کمکی نمی‌کنه.")
    elif worst["status"] == 429:
        print("[X] ریت‌لیمیت. یه ساعت صبر کن، بعد دوباره.")
    elif "html" in ctype:
        print("[X] به‌جای JSON صفحه HTML داده - صفحه بلاک یا چالش.")
        print("    راه‌حل: SHAZAM_PROXY.")
    elif worst["status"] == 200 and "json" in ctype:
        print("[OK] شزم JSON سالم داد. یعنی از سمت شبکه مشکلی نیست")
        print("     و اگه بازم تشخیص نمی‌ده، باگ سمت ماست - این خروجی رو بفرست.")
    elif worst["status"] >= 500:
        print("[!] خطای سمت خود شزم. موقتیه، بعدا دوباره امتحان کن.")
    else:
        print("[?] جواب غیرمنتظره. کل خروجی بالا رو بفرست.")

    if body:
        print()
        print("    ابتدای بدنه:")
        for line in body.splitlines()[:6]:
            print(f"      {line[:100]}")


async def main() -> int:
    audio = pick_audio()
    if audio is None:
        return 1

    from config import settings

    print(f"[*] فایل: {audio}  ({audio.stat().st_size // 1024}KB)")
    print(f"[*] پروکسی: {settings.shazam_proxy or 'ست نشده'}")
    print()

    calls = install_trace()

    from modules import recognize

    recognize.reset_client()
    try:
        song = await recognize._recognize_once(audio, attempts=1)
        print(f"[OK] نتیجه: {song.artist} - {song.title}" if song else "[*] تطبیقی پیدا نشد")
    except Exception as e:
        print(f"[X] {type(e).__name__}: {e}")

    print()
    print(f"[*] {len(calls)} درخواست HTTP:")
    for c in calls:
        print(f"    {c['status']:>3} {c['method']:<5} {c['url']}")
        print(f"        type={c['type']}  server={c['server']}  bytes={c['length']}")

    verdict(calls)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
