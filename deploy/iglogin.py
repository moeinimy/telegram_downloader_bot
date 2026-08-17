"""
Sign in to Instagram's MOBILE api once, and keep the session.

Why this exists as its own step.

Every credential this feature has used so far was a browser `sessionid`. That
cookie belongs to the WEB api: the web poller uses it happily, and the mobile
api answers it with `login_required` or Instagram's generic error page. So the
realtime channel - the only source that speaks to the mobile api, and the only
one that costs no requests at all - has never had a credential it could use.

A mobile session is the credential it wants. It is what the phone app holds,
it is native to that api, and it lasts months instead of hours. It can only be
created by an actual mobile sign-in, which is this script.

The session is written to the same file modules/ig_private.py has always used,
so the standby poller picks it up too. The device fingerprint is written
alongside it and reused forever after: changing the device Instagram sees is
itself what made the direct endpoints start refusing this account once before.

Run it through `botctl iglogin`, which supplies the venv and the right user.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from utils import proxies  # noqa: E402

SESSION_PATH = settings.download_dir / "ig_private_session.json"
DEVICE_PATH = settings.download_dir / "ig_device.json"


def _say(icon: str, text: str) -> None:
    print(f"{icon} {text}", flush=True)


def main() -> int:
    try:
        from instagrapi import Client
    except ImportError:
        _say("[X]", "instagrapi نصب نیست. اول: botctl igdirect → گزینه ۲")
        return 1

    username = settings.ig_dm_username or input("یوزرنیم: ").strip()
    if not username:
        _say("[X]", "یوزرنیم لازمه")
        return 1

    password = getpass.getpass(f"پسورد {username}: ")
    if not password:
        _say("[X]", "پسورد خالی بود")
        return 1

    client = Client()
    client.delay_range = [1, 3]

    # The device has to be the same one every time. A session is tied to the
    # device that created it, and generating a new fingerprint on each login
    # is the same mistake as sending a mismatched User-Agent with a cookie -
    # it reads as the account moving to a new phone.
    if DEVICE_PATH.exists():
        try:
            client.set_settings(json.loads(DEVICE_PATH.read_text(encoding="utf-8")))
            _say("[*]", "device قبلی استفاده شد")
        except Exception as e:
            _say("[!]", f"device قبلی خونده نشد ({e}) - یکی تازه ساخته می‌شه")

    proxy = proxies.normalize(settings.ig_dm_proxy)
    if proxy:
        client.set_proxy(proxy)
        _say("[*]", f"از پروکسی رد می‌شه: {proxy.split('@')[-1]}")
    else:
        _say("[*]", "بدون پروکسی - مستقیم از آدرس همین سرور")

    _say("[*]", "در حال لاگین…")
    try:
        client.login(username, password)
    except Exception as e:
        text = f"{type(e).__name__}: {e}"
        _say("[X]", text)
        lowered = text.lower()
        if "twofactor" in lowered or "two_factor" in lowered:
            _say("[!]", "این اکانت 2FA داره. فعلا خاموشش کن، لاگین کن، بعد دوباره روشن.")
        elif "challenge" in lowered or "checkpoint" in lowered:
            _say("[!]", "اینستاگرام تایید خواسته. تو اپ گوشی همون اکانت رو باز کن،")
            _say("[!]", "تاییدیه رو کامل کن، چند دقیقه صبر کن و دوباره اینو بزن.")
        elif "badpassword" in lowered or "bad password" in lowered:
            _say("[!]", "این همیشه یعنی پسورد غلط نیست - اینستاگرام لاگین از این")
            _say("[!]", "آدرس رو هم با همین جواب رد می‌کنه. اگه پسورد مطمئنا درسته،")
            _say("[!]", "یعنی آدرس سرور قبول نشده و یه پروکسی residential لازمه.")
        return 1

    # A login that returns without raising is not yet a session that works.
    try:
        client.get_timeline_feed()
    except Exception as e:
        _say("[X]", f"لاگین شد ولی سشن کار نمی‌کنه: {e}")
        return 1

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    client.dump_settings(str(SESSION_PATH))
    SESSION_PATH.chmod(0o600)
    DEVICE_PATH.write_text(json.dumps(client.get_settings()), encoding="utf-8")
    DEVICE_PATH.chmod(0o600)

    _say("[OK]", f"وارد شد به عنوان @{username}")
    _say("[OK]", f"سشن ذخیره شد: {SESSION_PATH}")
    print()
    _say("[*]", "این سشن مال api موبایله، پس realtime هم می‌تونه ازش استفاده کنه.")
    _say("[*]", "بعد از ریستارت تو لاگ دنبال این بگرد:")
    print("    ig mqtt: reused the stored mobile session")
    print("    ig mqtt: realtime connected - polling is no longer needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
