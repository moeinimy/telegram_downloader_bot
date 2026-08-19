"""
Sign in to Instagram's WEB api with a username and password, from the server.

Why this exists, after the mobile login was exhausted.

`botctl iglogin` signs in to the MOBILE api. From this address that route is
closed, and closed in two different ways depending on which app build asks:

    current build (428.x) -> ChallengeRequired, Bloks redirect. No code is
                             sent, approving it in the app does not clear it.
    older build (203.x)   -> BadPassword. Refused at the login context before
                             a challenge is even offered.

The web api is a different door. It is the one the account owner already walks
through in a browser, its checkpoint is the ordinary one that emails six
digits, and - the point - the cookie it produces is exactly what
modules/ig_web.py wants. The cookie route through `botctl igdirect` needs
somebody to open devtools and copy three values by hand; this asks for the
password once and produces the same three itself.

A dead cookie is a total outage here: all sources share one sessionid, so
`user_has_logged_out` takes the whole feature down and nothing fails over. A
password that can mint a fresh cookie is the thing that outage has been
missing.

Run it through `botctl igweblogin`, which supplies the venv and the right user.
"""

from __future__ import annotations

import getpass
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from utils import proxies  # noqa: E402

BASE = "https://www.instagram.com"
LOGIN_URL = f"{BASE}/api/v1/web/accounts/login/ajax/"
TWO_FACTOR_URL = f"{BASE}/api/v1/web/accounts/login/two_factor/"

# The public web-client id instagram.com sends with its own XHRs. Without it
# these endpoints answer 403 even with a perfectly good cookie - the same
# constant modules/ig_web.py sends on every request.
APP_ID = "936619743392459"

# A current desktop Chrome. This one is sent with the login AND handed back to
# be stored, because Instagram ties a session to the client that created it:
# the cookie and the browser that made it are one thing, not two.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _say(icon: str, text: str) -> None:
    print(f"{icon} {text}", flush=True)


def _headers(csrf: str, referer: str = f"{BASE}/accounts/login/") -> dict:
    return {
        "User-Agent": USER_AGENT,
        "X-IG-App-ID": APP_ID,
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def _enc(password: str) -> str:
    """The plaintext form instagram.com itself posts when it has no key.

    The web client encrypts with a key it fetches, but it keeps this
    unencrypted spelling for the case where it has none, and the endpoint
    still accepts it. The `0` is the key id that means "not encrypted".
    """
    return f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{password}"


def _client():
    import httpx

    proxy = proxies.normalize(settings.ig_dm_proxy)
    if proxy:
        _say("[*]", f"از پروکسی رد می‌شه: {proxy.split('@')[-1]}")
    else:
        _say("[*]", "بدون پروکسی - مستقیم از آدرس همین سرور")
    try:
        return httpx.Client(timeout=30, follow_redirects=True, proxy=proxy)
    except TypeError:  # httpx named it `proxies` before 0.26
        return httpx.Client(timeout=30, follow_redirects=True, proxies=proxy)


def _json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def _resolve_checkpoint(client, path: str, csrf: str) -> bool:
    """The web checkpoint: pick where the code goes, then post the code.

    This is the flow the account owner already sees in a browser, and the
    reason this script exists at all - the mobile checkpoint never offered a
    code to give.
    """
    url = path if path.startswith("http") else BASE + path
    _say("[!]", "اینستاگرام تایید خواسته - مسیر وب.")

    # Ask for the JSON view explicitly. Plain GET on this path answers with the
    # challenge PAGE - html - which parsed to nothing, so step_name came back
    # empty, the choice was never posted, and a code was then asked for that
    # nobody had requested. That is why no email ever arrived.
    body: dict = {}
    for params in ({"__a": "1", "__d": "dis"}, None):
        resp = client.get(url, headers=_headers(csrf, referer=url), params=params)
        csrf = client.cookies.get("csrftoken") or csrf
        body = _json(resp)
        if body:
            break
        _say("[!]", f"این شکل جواب json نداد ({resp.status_code}, "
                    f"{resp.headers.get('content-type', '?')[:40]})")

    # Which contact points this account can be reached at. The field names
    # differ between responses, so both spellings are looked for.
    step = body.get("step_name") or ""
    data = body.get("step_data") or {}
    if not data and isinstance(body.get("challenge"), dict):
        data = body["challenge"].get("step_data") or {}
    _say("[*]", f"مرحله: {step or '(خالی)'}  مقصدها: {sorted(data) or '(هیچی)'}")

    choice = "1" if "email" in data else ("0" if "phone_number" in data else "")
    if not choice and step in ("select_verify_method",
                               "select_contact_point_recovery"):
        choice = "1"

    if not choice:
        # Never prompt for a code that was never requested. The prompt is what
        # made this look like Instagram had gone quiet, when in fact it had
        # never been asked to send anything.
        _say("[X]", "نتونستم بفهمم کد رو کجا بفرسته، پس درخواستی هم نرفت.")
        _say("[!]", "برای همین هیچ کدی نمیاد - این انتظار بی‌مورده.")
        if body:
            _say("[!]", f"جواب اینستاگرام: "
                        f"{json.dumps(body, ensure_ascii=False)[:400]}")
        else:
            _say("[!]", "اینستاگرام json نداد - این چک‌پوینت صفحه‌ایه، نه api.")
        return False

    where = "ایمیل" if choice == "1" else "پیامک"
    _say("[*]", f"می‌خوام کد رو به {where} بفرسته…")
    resp = client.post(url, headers=_headers(csrf, referer=url),
                       data={"choice": choice})
    body = _json(resp)
    csrf = client.cookies.get("csrftoken") or csrf
    if resp.status_code >= 400:
        _say("[X]", f"درخواست کد رد شد ({resp.status_code}): "
                    f"{json.dumps(body, ensure_ascii=False)[:300]}")
        return False

    _say("[OK]", f"کد خواسته شد - باید به {where} برسه.")
    _say("[!]", "کد ۶ رقمی رو بردار.")
    code = input("کد (خالی = انصراف): ").strip()
    if not code:
        _say("[!]", "لغو شد.")
        return False

    resp = client.post(url, headers=_headers(csrf, referer=url),
                       data={"security_code": code})
    body = _json(resp)
    if resp.status_code >= 400 or body.get("status") == "fail":
        _say("[X]", f"کد قبول نشد: {json.dumps(body, ensure_ascii=False)[:300]}")
        return False

    _say("[OK]", "کد قبول شد.")
    return True


def _two_factor(client, username: str, info: dict, csrf: str) -> bool:
    identifier = info.get("two_factor_identifier")
    if not identifier:
        _say("[X]", "اینستاگرام شناسه‌ی دومرحله‌ای نداد.")
        return False
    _say("[!]", "این اکانت تایید دومرحله‌ای داره.")
    code = input("کد دومرحله‌ای (خالی = انصراف): ").strip()
    if not code:
        return False

    resp = client.post(
        TWO_FACTOR_URL,
        headers=_headers(csrf),
        data={"username": username, "verificationCode": code,
              "identifier": identifier},
    )
    body = _json(resp)
    if not body.get("authenticated"):
        _say("[X]", f"کد دومرحله‌ای قبول نشد: "
                    f"{json.dumps(body, ensure_ascii=False)[:300]}")
        return False
    return True


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None

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

    client = _client()

    # The login form first, only to be handed a csrftoken. Posting without one
    # is refused before the credentials are even looked at.
    _say("[*]", "دارم صفحه‌ی لاگین رو می‌گیرم…")
    try:
        client.get(f"{BASE}/accounts/login/", headers={"User-Agent": USER_AGENT})
    except Exception as e:
        _say("[X]", f"به اینستاگرام نرسیدم: {type(e).__name__}: {e}")
        return 1

    csrf = client.cookies.get("csrftoken") or ""
    if not csrf:
        _say("[X]", "csrftoken نداد - این آدرس احتمالا اصلا به صفحه نرسیده.")
        return 1

    _say("[*]", "در حال لاگین…")
    resp = client.post(
        LOGIN_URL,
        headers=_headers(csrf),
        data={"username": username, "enc_password": _enc(password),
              "queryParams": "{}", "optIntoOneTap": "false"},
    )
    body = _json(resp)
    csrf = client.cookies.get("csrftoken") or csrf

    if body.get("two_factor_required"):
        if not _two_factor(client, username, body.get("two_factor_info") or {}, csrf):
            return 1
    elif not body.get("authenticated"):
        message = str(body.get("message") or "")
        checkpoint = body.get("checkpoint_url") or ""
        if "checkpoint" in message or checkpoint:
            if not checkpoint:
                _say("[X]", "چک‌پوینت خورد ولی آدرسش رو نداد.")
                return 1
            if not _resolve_checkpoint(client, checkpoint, csrf):
                return 1
        elif body.get("user") is False:
            _say("[X]", f"همچین یوزرنیمی نیست: @{username}")
            return 1
        else:
            _say("[X]", f"لاگین نشد ({resp.status_code}): "
                        f"{json.dumps(body, ensure_ascii=False)[:400]}")
            if "password" in message.lower() or body.get("user"):
                _say("[!]", "اگه پسورد مطمئنا درسته، یعنی اینستاگرام لاگین از")
                _say("[!]", "این آدرس رو رد کرده و پروکسی residential لازمه.")
            return 1

    sessionid = client.cookies.get("sessionid") or ""
    ds_user_id = client.cookies.get("ds_user_id") or ""
    csrf = client.cookies.get("csrftoken") or csrf
    if not sessionid:
        _say("[X]", "لاگین قبول شد ولی sessionid نداد - چیزی برای ذخیره نیست.")
        return 1

    # Proof, not assumption: a login response is not the same thing as a
    # session the inbox endpoint will actually answer.
    _say("[*]", "دارم سشن رو روی خود اینباکس تست می‌کنم…")
    probe = client.get(
        f"{BASE}/api/v1/direct_v2/inbox/",
        headers=_headers(csrf, referer=f"{BASE}/direct/inbox/"),
        params={"limit": 1},
    )
    if probe.status_code != 200:
        _say("[X]", f"سشن ساخته شد ولی اینباکس {probe.status_code} داد.")
        _say("[!]", "کوکی ذخیره نشد، چون کار نمی‌کنه.")
        return 1
    _say("[OK]", "اینباکس با این سشن جواب داد.")

    if out_path:
        out_path.write_text(
            f"IG_DM_SESSIONID={sessionid}\n"
            f"IG_DM_CSRFTOKEN={csrf}\n"
            f"IG_DM_DS_USER_ID={ds_user_id}\n"
            f"IG_DM_USER_AGENT={USER_AGENT}\n",
            encoding="utf-8",
        )
        out_path.chmod(0o600)

    _say("[OK]", f"وارد شد به عنوان @{username}")
    _say("[*]", "این کوکی مال api وبه - همونی که منبع web می‌خواد.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
