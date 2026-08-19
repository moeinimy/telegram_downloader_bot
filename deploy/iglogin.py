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
# Which account that device belongs to. Without it a device is reused
# blindly, including across an account switch.
OWNER_PATH = settings.download_dir / "ig_device_owner.txt"


def _say(icon: str, text: str) -> None:
    print(f"{icon} {text}", flush=True)


def _code_handler(username: str, choice=None) -> str:
    """Ask for the six digits Instagram just sent.

    instagrapi ships a default handler that prompts in English and loops
    silently up to 24 times; this one names where the code went and lets an
    empty line stop, so a wrong guess is not answered by 23 more prompts.
    """
    where = "ایمیل" if str(getattr(choice, "value", choice)) in ("1", "EMAIL") else "پیامک"
    _say("[!]", f"اینستاگرام یه کد ۶ رقمی به {where} فرستاد.")
    code = input("کد (خالی = انصراف): ").strip()
    if not code:
        raise KeyboardInterrupt("cancelled by user")
    return code


def _resolve_with_code(client, username: str) -> bool:
    """Drive the code challenge that instagrapi declines to attempt.

    Reading its source rather than guessing: challenge_resolve() raises on
    sight of a native_flow checkpoint - before any handler runs, which is why
    no code was ever sent and the account owner was never asked for one. Only
    the entry point opts out. challenge_resolve_simple() underneath it does
    implement the whole flow: pick email, wait for the code, post it back.

    So the guard is stepped around rather than the flow reimplemented. If
    Instagram genuinely will not take a code for this checkpoint, that shows
    up as an error from the real endpoint instead of from a library that never
    asked.
    """
    data = getattr(client, "last_json", None) or {}
    api_path = (data.get("challenge") or {}).get("api_path") or ""
    if not api_path:
        _say("[!]", "اینستاگرام آدرس چلنج نداد - مسیر کد در دسترس نیست.")
        return False
    try:
        url = client._normalize_challenge_api_path(api_path)
    except Exception:
        url = api_path
    _say("[*]", "دارم مسیر کد رو باز می‌کنم…")
    try:
        client.challenge_resolve_simple(url)
        return True
    except KeyboardInterrupt:
        _say("[!]", "لغو شد.")
        return False
    except Exception as e:
        _say("[X]", f"مسیر کد جواب نداد: {type(e).__name__}: {e}")
        return False


def main() -> int:
    try:
        from instagrapi import Client
    except ImportError:
        _say("[X]", "instagrapi نصب نیست. اول: botctl igdirect → گزینه ۲")
        return 1

    # IG_DM_USERNAME is shown rather than used silently. This command exists
    # mainly to move to a NEW account, and .env still holds the old one at
    # that moment - signing into the account being replaced, without ever
    # printing its name, is the one outcome that looks like success.
    username = settings.ig_dm_username
    if username:
        _say("[*]", f"IG_DM_USERNAME تو .env: @{username}")
        typed = input("Enter برای همین، یا یوزرنیم جدید رو بنویس: ").strip()
        username = typed or username
    else:
        username = input("یوزرنیم: ").strip()
    if not username:
        _say("[X]", "یوزرنیم لازمه")
        return 1

    password = getpass.getpass(f"پسورد {username}: ")
    if not password:
        _say("[X]", "پسورد خالی بود")
        return 1

    client = Client()
    client.delay_range = [1, 3]

    # One account, one phone - in both directions.
    #
    # The device must not change between logins of the SAME account: a session
    # is tied to the device that made it, and a new fingerprint each time
    # reads as the account moving to a new phone, which is what made the
    # direct endpoints start refusing us once before.
    #
    # And it must not be shared with a DIFFERENT account. Instagram associates
    # accounts through the device they sign in from, so handing a fresh
    # account the fingerprint of the ones that were already checkpointed on it
    # links the new one to them and burns it on arrival.
    # `botctl iglogin reset` - the way out when the saved device is known to be
    # wrong and no question is going to establish that. Deliberately explicit:
    # throwing the device away is normally the mistake, not the fix.
    if "reset" in sys.argv[1:]:
        if DEVICE_PATH.exists() or OWNER_PATH.exists():
            _say("[*]", "device قبلی پاک شد - یکی تازه ساخته می‌شه")
        DEVICE_PATH.unlink(missing_ok=True)
        OWNER_PATH.unlink(missing_ok=True)

    owner = OWNER_PATH.read_text(encoding="utf-8").strip() if OWNER_PATH.exists() else ""

    # An unrecorded owner is not evidence that the device is ours. The owner
    # file only starts existing after the first successful login through this
    # script, so on the very run that matters most - the first one after
    # switching accounts - "no owner" was being read as "same account" and the
    # retired account's fingerprint was reused in silence. Unknown is asked
    # about, not assumed.
    if DEVICE_PATH.exists() and not owner:
        _say("[!]", "یه device ذخیره‌شده هست ولی معلوم نیست مال کدوم اکانته.")
        _say("[!]", "اگه مال اکانت قبلیه، استفاده ازش این دو تا رو به هم وصل می‌کنه.")
        keep = input(f"این device مال @{username} خودشه؟ (y/n) ").strip().lower()
        if keep != "y":
            _say("[*]", "device تازه ساخته می‌شه")
            DEVICE_PATH.unlink(missing_ok=True)
        else:
            owner = username

    if DEVICE_PATH.exists() and owner and owner.lower() != username.lower():
        _say("[!]", f"device ذخیره‌شده مال @{owner} بود، نه @{username}.")
        _say("[!]", "برای اکانت جدید device تازه ساخته می‌شه - اشتراک گذاشتنش")
        _say("[!]", "همون چیزیه که دو اکانت رو به هم وصل می‌کنه.")
        DEVICE_PATH.unlink(missing_ok=True)
    elif DEVICE_PATH.exists():
        try:
            client.set_settings(json.loads(DEVICE_PATH.read_text(encoding="utf-8")))
            _say("[*]", "device قبلی استفاده شد")
        except Exception as e:
            _say("[!]", f"device قبلی خونده نشد ({e}) - یکی تازه ساخته می‌شه")

    # Saved BEFORE the login, not after it.
    #
    # Instagram's own challenge text asks for exactly this: "Retry with the
    # same saved client settings, device identifiers, and proxy/IP." The
    # device was only written on success, so every failed attempt was followed
    # by a retry from a device Instagram had never seen - a different phone
    # each time, at the same address, against the same account. That is its
    # own reason to challenge, so the retries could not have cleared the
    # challenge they were retrying.
    #
    # Written once here and reused from now on, whether or not this attempt
    # gets through.
    if not DEVICE_PATH.exists():
        DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEVICE_PATH.write_text(json.dumps(client.get_settings()), encoding="utf-8")
        DEVICE_PATH.chmod(0o600)
        OWNER_PATH.write_text(username, encoding="utf-8")
        OWNER_PATH.chmod(0o600)
        _say("[*]", f"device تازه ساخته و ذخیره شد - همین برای @{username} می‌مونه")

    proxy = proxies.normalize(settings.ig_dm_proxy)
    if proxy:
        client.set_proxy(proxy)
        _say("[*]", f"از پروکسی رد می‌شه: {proxy.split('@')[-1]}")
    else:
        _say("[*]", "بدون پروکسی - مستقیم از آدرس همین سرور")

    client.challenge_code_handler = _code_handler

    _say("[*]", "در حال لاگین…")
    text = ""
    try:
        client.login(username, password)
    except Exception as e:
        text = f"{type(e).__name__}: {e}"

    # The checkpoint that never asked for a code. Try the code path once, then
    # sign in again - resolving a challenge does not itself log you in.
    if text and ("challenge" in text.lower() or "checkpoint" in text.lower()):
        _say("[!]", "چک‌پوینت خورد. قبل از تسلیم شدن، مسیر کد رو امتحان می‌کنم.")
        if _resolve_with_code(client, username):
            _say("[OK]", "چلنج قبول شد - دوباره لاگین می‌کنم…")
            text = ""
            try:
                client.login(username, password)
            except Exception as e2:
                text = f"{type(e2).__name__}: {e2}"

    if text:
        _say("[X]", text)
        lowered = text.lower()
        if "twofactor" in lowered or "two_factor" in lowered:
            _say("[!]", "این اکانت 2FA داره. فعلا خاموشش کن، لاگین کن، بعد دوباره روشن.")
        elif "challenge" in lowered or "checkpoint" in lowered:
            _say("[!]", "اینستاگرام تایید خواسته. تو اپ گوشی همون اکانت رو باز کن،")
            _say("[!]", "تاییدیه رو کامل کن، چند دقیقه صبر کن و دوباره اینو بزن.")
            # The two things that decide whether the retry can ever work, and
            # neither is obvious from Instagram's own wording.
            if not proxy:
                _say("[!]", "")
                _say("[!]", "ولی این لاگین از آدرس خود سرور رفت، که دیتاسنتره.")
                _say("[!]", "یه چلنج که از اینجا خورده، از اینجا هم دوباره می‌خوره -")
                _say("[!]", "تایید کردن تو گوشی آدرس رو عوض نمی‌کنه. اگه بار دوم هم")
                _say("[!]", "همینو داد، جوابش پروکسی residential ـه: botctl proxy")
            _say("[!]", "")
            _say("[!]", "و اگه این اکانت قبلا چک‌پوینت خورده، این همون چک‌پوینت")
            _say("[!]", "بازه - نه یه تایید تازه. اون اکانت با تلاش دوباره برنمی‌گرده.")
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
    OWNER_PATH.write_text(username, encoding="utf-8")
    OWNER_PATH.chmod(0o600)

    _say("[OK]", f"وارد شد به عنوان @{username}")
    _say("[OK]", f"سشن ذخیره شد: {SESSION_PATH}")
    print()

    # The saved session is only the FIRST route modules/ig_private.py tries.
    # If it is ever rejected, route 2 is IG_DM_SESSIONID and route 3 is
    # IG_DM_USERNAME/PASSWORD - both still describing the old account. That
    # would sign the old account in on the new account's device, which is the
    # association this whole command is arranged to avoid.
    if settings.ig_dm_username and settings.ig_dm_username.lower() != username.lower():
        _say("[!]", f".env هنوز روی @{settings.ig_dm_username} ست شده.")
        _say("[!]", "تا عوضش نکنی، هر بار سشن رد بشه برمی‌گرده به اکانت قبلی")
        _say("[!]", "و اون رو روی device این اکانت لاگین می‌کنه. عوض کن:")
        print(f"    IG_DM_USERNAME={username}")
        print( "    IG_DM_PASSWORD=<پسورد همین اکانت>")
        print( "    IG_DM_SESSIONID=        <- خالی، مال اکانت قبلیه")
        print()
    _say("[*]", "این سشن مال api موبایله، پس realtime هم می‌تونه ازش استفاده کنه.")
    _say("[*]", "بعد از ریستارت تو لاگ دنبال این بگرد:")
    print("    ig mqtt: reused the stored mobile session")
    print("    ig mqtt: realtime connected - polling is no longer needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
