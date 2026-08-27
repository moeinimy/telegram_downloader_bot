"""botctl igtest <username> - hit every endpoint the IG buttons need.

Written as a file rather than inline so the shell never has to quote Python.
"""
import sys

sys.path.insert(0, ".")

from modules import ig_web, ig_stories  # noqa: E402

USERNAME = (sys.argv[1] if len(sys.argv) > 1 else "instagram").lstrip("@")
BASE = "https://www.instagram.com/api/v1"

G, R, Y, N = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0m"


def line(label, ok, detail=""):
    mark = f"{G}OK {N}" if ok is True else (
        f"{R}X  {N}" if ok is False else f"{Y}?  {N}")
    print(f"  {mark} {label:34} {detail}")


print()
print("=== وضعیت سشن اینستاگرام ===")
print()

if not ig_web.usable():
    line("sessionid", False, "تنظیم نشده — botctl igdirect")
    raise SystemExit(1)
line("sessionid", True, "تنظیم شده")

try:
    r = ig_web.rate()
    line("rate", None, str(r)[:70])
except Exception as e:
    line("rate", None, str(e)[:60])

print()
print(f"=== اندپوینت‌ها، روی @{USERNAME} ===")
print()

uid = ""
for name, url, params in (
    ("web_profile_info", f"{BASE}/users/web_profile_info/",
     {"username": USERNAME}),
    ("topsearch", "https://www.instagram.com/web/search/topsearch/",
     {"context": "blended", "query": USERNAME}),
):
    try:
        data = ig_web.get(url, params,
                          referer=f"https://www.instagram.com/{USERNAME}/")
        keys = list((data or {}).keys())[:4]
        line(name, True, f"keys={keys}")
    except Exception as e:
        line(name, False, str(e)[:90])

try:
    user = ig_stories.profile(USERNAME)
    uid = user["id"]
    line("resolved the user", True,
         f"id={uid} private={user.get('is_private')}")
except Exception as e:
    line("resolved the user", False, str(e).replace("\n", " | ")[:90])

if uid:
    for name, url, params in (
        ("users/<id>/info/", f"{BASE}/users/{uid}/info/", {}),
        ("feed/reels_media/ (stories)", f"{BASE}/feed/reels_media/",
         {"reel_ids": uid}),
        ("highlights_tray", f"{BASE}/highlights/{uid}/highlights_tray/", {}),
    ):
        try:
            data = ig_web.get(url, params,
                              referer=f"https://www.instagram.com/{USERNAME}/")
            if "reels_media" in url:
                trays = (data or {}).get("reels_media") or []
                n = len((trays[0].get("items") or [])) if trays else 0
                line(name, True, f"{n} استوری")
            elif "highlights_tray" in url:
                line(name, True, f"{len((data or {}).get('tray') or [])} هایلایت")
            else:
                line(name, True, f"keys={list((data or {}).keys())[:3]}")
        except Exception as e:
            line(name, False, str(e)[:90])

print()
print("هر خط X رو همینجوری کپی کن و بفرست.")
print()
