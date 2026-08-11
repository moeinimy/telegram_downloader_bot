"""
What does Instagram actually send back to THIS server?

The bot's log shows the end of the story - "sweep failed", "login_required",
a 403 buried in a traceback. This runs the same sequence deliberately and
reports each step, so a refused address, a stale session, a rejected device
and a genuine account problem stop looking alike.

    botctl igtest

Every step prints what it tried, what came back, and what that means. It
changes nothing: no session is deleted, no setting is written.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OK, BAD, INFO = "[OK]", "[X] ", "[*] "


def head(title: str) -> None:
    print()
    print("=" * 60)
    print(f"{INFO}{title}")
    print("=" * 60)


def main() -> int:
    from config import settings

    head("تنظیمات")
    print(f"  یوزرنیم        : {settings.ig_dm_username or '(ست نشده)'}")
    print(f"  sessionid      : {'ست شده' if settings.ig_dm_sessionid else '(ست نشده)'}")
    print(f"  پسورد          : {'ست شده' if settings.ig_dm_password else '(ست نشده)'}")
    print(f"  پروکسی         : {settings.ig_dm_proxy or '(بدون پروکسی)'}")
    print(f"  فاصله پولینگ   : {settings.ig_dm_poll_seconds}s / {settings.ig_dm_fast_seconds}s")

    try:
        from modules import ig_private
    except Exception as e:
        print(f"{BAD}ماژول لود نشد: {e}")
        return 1

    if not ig_private.available():
        print(f"{BAD}instagrapi نصب نیست. botctl igdirect")
        return 1

    print(f"  session file   : {'هست' if ig_private._SESSION_PATH.exists() else 'نیست'}")
    print(f"  device file    : {'هست' if ig_private._DEVICE_PATH.exists() else 'نیست'}")

    # ---- 1. can we reach Instagram at all, and from which address? ----
    head("۱) دسترسی شبکه")
    import requests

    proxies = {"http": settings.ig_dm_proxy, "https": settings.ig_dm_proxy} \
        if settings.ig_dm_proxy else None

    for label, url in (("بدون پروکسی", None), ("با پروکسی", settings.ig_dm_proxy)):
        if url is None and proxies:
            use = None
        elif url is None:
            use = None
        else:
            use = proxies
        if label == "با پروکسی" and not proxies:
            continue
        try:
            r = requests.get("https://api.ipify.org", proxies=use, timeout=15)
            print(f"  IP خروجی ({label}): {r.text.strip()}")
        except Exception as e:
            print(f"  {BAD}IP خروجی ({label}): {type(e).__name__}: {e}")

    try:
        r = requests.get("https://i.instagram.com/api/v1/", proxies=proxies, timeout=15)
        marker = OK if r.status_code != 403 else BAD
        print(f"  {marker}i.instagram.com -> HTTP {r.status_code}  ({r.headers.get('content-type','?')})")
        if r.status_code == 403:
            print("      ↳ آدرسی که ازش درخواست می‌ره رد شده. پروکسی لازمه: botctl proxy")
    except Exception as e:
        print(f"  {BAD}i.instagram.com -> {type(e).__name__}: {e}")

    # ---- 2. sign in ----
    head("۲) لاگین")
    ig_private.clear_block()
    try:
        client = ig_private.client()
        print(f"  {OK}لاگین موفق. user_id={getattr(client, 'user_id', '?')}")
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        print(f"  {BAD}{text[:300]}")
        print()
        if settings.ig_dm_sessionid and settings.ig_dm_proxy:
            print("      ↳ کوکی sessionid به IP‌ای که باهاش ساخته شده گره می‌خوره.")
            print("        اگه قبل از ست کردن پروکسی کار می‌کرد و حالا نه، کوکی")
            print("        باطل شده چون کشور خروجی عوض شده - نه اینکه پروکسی خرابه.")
            print()
            print("        کاری که باید بکنی، به همین ترتیب:")
            print("          ۱. پروکسی رو نگه دار (همین که هست)")
            print("          ۲. یه sessionid *تازه* از مرورگر بگیر")
            print("          ۳. botctl igdirect → گزینه ۲ → sessionid جدید")
        elif "403" in text or "login_required" in text:
            print("      ↳ لاگین رد شد، نه اکانت.")
            print("        اگه پروکسی نداری: IP این سرور قبول نیست → botctl proxy")
        return 1

    # ---- 3. the call that actually matters ----
    head("۳) خوندن اینباکس")
    try:
        client.direct_threads(amount=3, thread_message_limit=3)
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        print(f"  {BAD}{text[:400]}")
        if "1404006" in text or "item_ack" in text:
            print()
            print("      ↳ اینستاگرام درخواست دایرکت رو رد کرد ولی لاگین قبول شد.")
            print("        معمولا یعنی IP یا device برای این اکانت ناشناسه.")
            print("        امتحان کن: botctl proxy  (و device رو پاک نکن)")
        return 1

    raw = client.last_json or {}
    threads = ((raw.get("inbox") or {}).get("threads")) or []
    print(f"  {OK}اینباکس خونده شد - {len(threads)} thread")

    for thread in threads[:3]:
        items = thread.get("items") or []
        who = ", ".join(u.get("username", "?") for u in (thread.get("users") or [])[:2])
        print(f"     • {who or '?'} - {len(items)} پیام")
        for item in items[:2]:
            permalink, pk, url = ig_private._media_from_item(item)
            kind = item.get("item_type", "?")
            found = permalink or pk or url or (item.get("text") or "")[:40] or "(چیزی استخراج نشد)"
            print(f"        [{kind}] {str(found)[:80]}")

    # ---- 4. pending inbox ----
    head("۴) درخواست‌های پیام (pending)")
    try:
        client.direct_pending_inbox(amount=3)
        pending = (((client.last_json or {}).get("inbox") or {}).get("threads")) or []
        print(f"  {OK}{len(pending)} درخواست در انتظار")
    except Exception as e:
        print(f"  {BAD}{type(e).__name__}: {str(e)[:200]}")
        print("      ↳ مهم نیست اگه اینباکس اصلی کار کرد؛ فقط کاربرای جدید دیرتر دیده می‌شن.")

    head("نتیجه")
    print(f"  {OK}دایرکت کار می‌کنه. اگه بات هنوز چیزی نمی‌فرسته:")
    print("     botctl logs   و دنبال 'ig poll' بگرد")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
