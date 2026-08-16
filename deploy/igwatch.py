"""
Watch the DM pipeline live, one stage at a time.

"Nothing arrives" can mean six different things, and from the outside they
look identical: the inbox is empty, the request is refused, the timestamp
filter drops everything, the sender is not paired, the media cannot be
extracted, or the delivery to Telegram fails.

This runs the same read the bot's poll loop runs, on the same schedule, and
prints what happens at every stage:

    botctl igwatch [seconds]

Send a DM to the bot's Instagram account while it runs. Whatever stage it
stops at is the answer.

Read-only: no state is written, the running bot is not disturbed, and the
high-water mark used here is this tool's own.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _age(seconds: float) -> str:
    if seconds < 0 or seconds > 10 * 365 * 86400:
        return "!! نامعتبر"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


async def main() -> int:
    from config import settings
    from modules import ig_items, ig_pairing, ig_web

    duration = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 120

    if not settings.has_ig_web:
        print("[X] کوکی‌های وب ست نشدن. botctl igdirect")
        return 1

    # Genuinely read-only, which the first version was not.
    #
    # _get persists the cookie jar after every request, so this tool and the
    # running bot would both be writing downloads/ig_web_cookies.json. If
    # Instagram rotated the sessionid while both were mid-flight, one would
    # overwrite the other's copy with a stale one and kill the session - the
    # exact failure this tool exists to investigate, caused by the tool.
    #
    # The rate file is left alone for the same reason: two writers lose data,
    # and a diagnostic must not corrupt the record it is there to read. These
    # few requests therefore go uncounted, which is the right way round.
    ig_web._save_cookies = lambda *_a, **_kw: None
    ig_web._count_request = lambda *_a, **_kw: None

    links = ig_pairing.count()
    print(f"[*] {links} اکانت وصل · پروکسی {settings.ig_dm_proxy or 'ندارد'}")
    print(f"[*] {duration} ثانیه گوش می‌دم.")
    print()
    print("    ⇩ همین حالا یه ریلز به پیج بات دایرکت کن ⇩")
    print()
    print(f"    {'زمان':<9}{'خونده':>7}{'تازه':>7}{'قابل‌تحویل':>12}   جدیدترین پیام")
    print("    " + "-" * 66)

    # This tool's own mark, so it does not disturb the bot's and so anything
    # sent from now on is visibly "new".
    started = time.time()
    mark = started
    deadline = started + duration
    delivered = 0
    reads = 0

    while time.time() < deadline:
        stamp = time.strftime("%H:%M:%S")
        try:
            raw = await asyncio.to_thread(
                ig_web._get, ig_web.INBOX,
                {"visual_message_return_type": "unseen", "thread_message_limit": 5,
                 "persistentBadging": "true", "limit": 8},
            )
        except Exception as e:
            text = str(e)
            print(f"    {stamp}  [X] {text[:60]}")
            if ig_web._is_soft_block(text):
                print()
                print("    ↳ اینستاگرام داره محدود می‌کنه. این همون لیمیتیه که حس کردی.")
                print("      IG_DM_MAX_INTERVAL رو ببر بالاتر و چند ساعت صبر کن.")
                return 1
            await asyncio.sleep(10)
            continue

        reads += 1
        items = [i for t in ((raw.get("inbox") or {}).get("threads") or [])
                 for i in (t.get("items") or [])]
        me = settings.ig_dm_ds_user_id

        newest_age, newest_note = None, "—"
        fresh = deliverable = 0
        for item in items:
            ts = ig_items.to_epoch(item.get("timestamp"))
            age = time.time() - ts
            if newest_age is None or age < newest_age:
                newest_age = age
                kind = item.get("item_type", "?")
                sender = str(item.get("user_id") or "")
                mine = " (خودمون)" if sender == me else ""
                newest_note = f"[{kind}]{mine}"
            if ts > mark:
                fresh += 1
                if ig_items.to_direct_message(item, "web", me):
                    deliverable += 1

        delivered += deliverable
        age_text = _age(newest_age) if newest_age is not None else "—"
        flag = " ←" if deliverable else ""
        print(f"    {stamp}  {len(items):>6}{fresh:>7}{deliverable:>12}   "
              f"{age_text:<6} {newest_note}{flag}")

        if deliverable:
            for item in items:
                ts = ig_items.to_epoch(item.get("timestamp"))
                if ts <= mark:
                    continue
                message = ig_items.to_direct_message(item, "web", me)
                if not message:
                    continue
                identity = message.identity()
                chat = ig_pairing.chat_for(identity)
                print(f"      • {identity} -> "
                      + (f"چت {chat}" if chat else "وصل نشده (باید کد pairing بفرسته)"))
                target = message.shortcode() or message.media_id or message.media_url
                print(f"        مدیا: {target[:60] if target else 'استخراج نشد — ' + str(message.raw)[:80]}")
            mark = time.time()

        await asyncio.sleep(max(5.0, settings.ig_dm_poll_seconds))

    print()
    print("    " + "-" * 66)
    if not reads:
        print("[X] حتی یه بار هم اینباکس خونده نشد - مشکل شبکه یا کوکیه.")
        return 1
    if delivered:
        print(f"[OK] {delivered} پیام قابل تحویل دیده شد. زنجیره تا اینجا سالمه.")
        print("     اگه تو تلگرام نیومد، مشکل تو دانلود یا آپلوده: botctl logs")
        return 0

    print(f"[!] {reads} بار خونده شد و هیچ پیام تازه‌ای نبود.")
    print()
    print("    یعنی اینستاگرام پیامی که فرستادی رو تو این اینباکس نشون نمی‌ده.")
    print("    محتمل‌ترین دلیل‌ها، به ترتیب:")
    print("      • فرستنده پیج رو فالو نکرده -> پیام تو «درخواست‌ها» مونده")
    print("      • با خود اکانت بات فرستادی (پیام خودمون شمرده نمی‌شه)")
    print("      • اینستاگرام برای این نشست دایرکت رو محدود کرده")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
