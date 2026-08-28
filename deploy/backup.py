"""Everything that makes this server THIS bot, in one file.

What a move actually loses, if you only copy the code.

The code is in git and comes back with a clone. What does not is the state
this instance accumulated, and some of it cannot be regenerated at all:

  .env                     the bot token, the admin ids, the cache channel,
                           every proxy and cookie setting. Without it the
                           bot is a different bot.
  file_ids.json            the cache. Every track this bot has ever sent has
                           a Telegram file_id here, and a cached track is
                           delivered in under a second instead of being
                           downloaded again. It is small and it is the single
                           most valuable file in the list - a fresh server
                           without it feels exactly like the bot did on its
                           first day.
  stats.db                 users, download history, who blocked the bot, and
                           each chat's language choice.
  ig_web_cookies.json      the LIVE Instagram session, including the
                           sessionid Instagram has rotated since login.
  ig_device.json           the device identity that session was issued to.
  ig_device_owner.txt      Instagram ties a session to the client that
                           created it. Restoring the cookie without the
                           device is a session appearing on a new machine,
                           which is what a checkpoint is for.
  ig_private_session.json  the instagrapi session.
  ig_pairing.json          who is paired to which Instagram account.
  ig_seen.json             which direct messages were already handled, so a
                           restore does not re-deliver a week of them.
  ig_token.json            the Graph api token.
  youtube_cookies.txt      if one was installed.

Deliberately NOT included: the downloaded media itself - instagram/,
spotify/, recognize/, thumbs/, q/, whisper/, .ytdlp-cache. Those are
working files that rebuild on demand and would turn a ~1MB archive into
gigabytes. What matters about a past download is its file_id, and that is
in file_ids.json.

yt_clients.json and yt_probes.json are included because they are tiny, but
they are the one part that is only an optimisation: they re-learn
themselves within a few downloads.

THE ARCHIVE IS A CREDENTIAL. It carries the bot token and a live Instagram
session; anyone holding it holds the bot. It is written 0600 and it belongs
somewhere private.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Files taken from the download directory. Missing ones are skipped without
# complaint - a bot that never set up Instagram has no Instagram state, and
# that is not an error.
STATE_FILES = (
    "file_ids.json",
    "stats.db",
    "ig_web_cookies.json",
    "ig_web_rate.json",
    "ig_device.json",
    "ig_device_owner.txt",
    "ig_private_session.json",
    "ig_pairing.json",
    "ig_seen.json",
    "ig_token.json",
    "yt_clients.json",
    "yt_probes.json",
)

# Files taken from the project directory.
PROJECT_FILES = (".env", "youtube_cookies.txt")

G, R, Y, N = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0m"


def _say(mark, colour, text):
    print(f"  {colour}{mark}{N} {text}")


def _download_dir(env_path: Path) -> Path:
    """Read DOWNLOAD_DIR out of a .env without importing config.

    config validates a great deal more than this needs and refuses to load
    at all when something is missing - which is exactly the situation a
    restore is meant to fix.
    """
    default = PROJECT_DIR / "downloads"
    if not env_path.is_file():
        return default
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("DOWNLOAD_DIR="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return Path(value)
    return default


def _rewrite_download_dir(env_path: Path, value: Path) -> None:
    """Point a restored .env at this machine's download directory."""
    lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, seen = [], False
    for line in lines:
        if line.strip().startswith("DOWNLOAD_DIR="):
            out.append(f"DOWNLOAD_DIR={value}")
            seen = True
        else:
            out.append(line)
    if not seen:
        out.append(f"DOWNLOAD_DIR={value}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _copy_sqlite(src: Path, dest: Path) -> None:
    """Copy a live database without risking a torn read.

    stats.db is being written while this runs. A plain file copy can catch
    it mid-transaction and produce an archive whose database will not open -
    which is discovered on the new server, on the day it is needed.
    sqlite's own backup api takes a consistent snapshot of a busy database.
    """
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source:
        with sqlite3.connect(dest) as target:
            source.backup(target)


def create(out_path: Path) -> int:
    env = PROJECT_DIR / ".env"
    dl = _download_dir(env)
    print()
    print("=== ساختن بکاپ ===")
    print()

    staged = Path(tempfile.mkdtemp(prefix="botbackup-"))
    included = 0
    try:
        for name in PROJECT_FILES:
            src = PROJECT_DIR / name
            if not src.is_file():
                _say("-", Y, f"{name} (نیست، رد شد)")
                continue
            shutil.copy2(src, staged / name)
            included += 1
            _say("OK", G, f"{name}  ({src.stat().st_size:,} bytes)")

        for name in STATE_FILES:
            src = dl / name
            if not src.is_file():
                _say("-", Y, f"{name} (نیست، رد شد)")
                continue
            dest = staged / name
            if name.endswith(".db"):
                _copy_sqlite(src, dest)
            else:
                shutil.copy2(src, dest)
            included += 1
            _say("OK", G, f"{name}  ({src.stat().st_size:,} bytes)")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, "w:gz") as tar:
            for item in sorted(staged.iterdir()):
                tar.add(item, arcname=item.name)
        os.chmod(out_path, 0o600)
    finally:
        shutil.rmtree(staged, ignore_errors=True)

    print()
    print(f"  {G}آماده:{N} {out_path}")
    print(f"  {included} فایل، {out_path.stat().st_size:,} بایت")
    print()
    print(f"  {R}این فایل خودِ باته.{N} توکن بات و سشن زنده‌ی اینستاگرام توشه.")
    print("  هرکی داشته باشدش، بات رو داره. جای امن نگهش دار.")
    print()
    print("  بردن روی سرور جدید:")
    print(f"    scp root@THIS:{out_path} .")
    print("    scp <file> root@NEW:/root/")
    print("    # روی سرور جدید، بعد از نصب:")
    print("    botctl restore /root/<file>")
    print()
    return 0


def restore(archive: Path) -> int:
    if not archive.is_file():
        print(f"{R}فایل پیدا نشد:{N} {archive}")
        return 1

    print()
    print("=== برگرداندن بکاپ ===")
    print()

    staged = Path(tempfile.mkdtemp(prefix="botrestore-"))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                # A tar can name "../../etc/passwd". Nothing here is ever a
                # path, so anything that is one is not our archive.
                if member.name != Path(member.name).name or not member.isfile():
                    print(f"{R}ورودی مشکوک در آرشیو:{N} {member.name}")
                    return 1
            tar.extractall(staged)

        # Where the state goes is a question about THIS machine, and the
        # answer is not in the archive.
        #
        # The archive's .env carries the old server's DOWNLOAD_DIR. Trusting
        # it works only while both machines are installed at the same path,
        # and when they are not it writes the cache and the sessions
        # somewhere this bot will never read - silently, with every file
        # reported restored. That is the worst way for a backup to fail:
        # it looks like it worked, and is discovered later.
        #
        # So the local install decides the path, and the restored .env is
        # rewritten to agree with it. Everything else in that file - the
        # token, the admin ids, the cache channel, the proxies - comes from
        # the archive untouched, because that is what identifies the bot.
        env_in_archive = staged / ".env"
        local_dl = _download_dir(PROJECT_DIR / ".env")
        archive_dl = _download_dir(env_in_archive) if env_in_archive.is_file() \
            else local_dl

        dl = local_dl
        if env_in_archive.is_file() and archive_dl != local_dl:
            _rewrite_download_dir(env_in_archive, local_dl)
            _say("!", Y, f"DOWNLOAD_DIR در بکاپ {archive_dl} بود — "
                         f"برای این سرور {local_dl} شد")
        dl.mkdir(parents=True, exist_ok=True)

        restored = 0
        for item in sorted(staged.iterdir()):
            target_dir = PROJECT_DIR if item.name in PROJECT_FILES else dl
            dest = target_dir / item.name
            if dest.exists():
                shutil.copy2(dest, dest.with_suffix(dest.suffix + ".before-restore"))
            shutil.copy2(item, dest)
            # .env and every session file is a credential.
            os.chmod(dest, 0o600 if item.name in PROJECT_FILES
                     or "cookie" in item.name or "session" in item.name
                     or "token" in item.name else 0o644)
            restored += 1
            _say("OK", G, f"{item.name} -> {target_dir}")

        print()
        print(f"  {restored} فایل برگشت. فایل‌های قبلی با پسوند "
              f".before-restore کنارشون موندن.")
        print()
        print("  حالا:  botctl restart   و بعد   botctl selfcheck")
        print()
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: backup.py create|restore <path>")
        raise SystemExit(2)
    action, path = sys.argv[1], Path(sys.argv[2])
    raise SystemExit(create(path) if action == "create" else restore(path))
