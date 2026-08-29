#!/usr/bin/env bash
# ==========================================================
#  Telegram Downloader Bot - manager
#  Run:  bash deploy/manage.sh      (or just: botctl)
# ==========================================================

set -uo pipefail

REPO_URL="https://github.com/moeinimy/telegram_downloader_bot.git"
PROJECT_DIR="/opt/telegram_downloader_bot"
SERVICE_NAME="tg-downloader-bot"
BOT_USER="botuser"

# Every python entrypoint here calls load_dotenv() and resolves DOWNLOAD_DIR
# with Path("./downloads").resolve() - both of which read the WORKING
# DIRECTORY, not the script's location. Run from /root, as any admin does,
# that meant .env was never found and downloads resolved to /root/downloads,
# which botuser cannot even stat:
#
#     PermissionError: [Errno 13] Permission denied: '/root/downloads'
#
# So the directory is part of the invocation rather than an assumption about
# where whoever typed botctl happened to be standing. sh -c rather than
# `env -C` because that flag is not in every coreutils this may meet.
run_py() {
    sudo -u "$BOT_USER" sh -c 'cd "$1" && shift && exec "$@"' \
        _ "$PROJECT_DIR" "$PROJECT_DIR/.venv/bin/python" "$@"
}


run_py_root() {
    # For the two things that are root's job rather than the bot's: reading
    # every state file regardless of owner, and writing into /root. The bot
    # user can do neither, so backup as botuser died on
    #
    #     PermissionError: '/root/moeinimydl-backup-....tar.gz'
    #
    # after successfully collecting all thirteen files.
    ( cd "$PROJECT_DIR" && exec "$PROJECT_DIR/.venv/bin/python" "$@" )
}


# Set by do_reset so do_install can put the user's files back after a wipe.
RESTORE_DIR=""

G="\033[0;32m"; R="\033[0;31m"; Y="\033[1;33m"; B="\033[0;36m"; N="\033[0m"

ok()   { echo -e "${G}[OK]${N} $*"; }
err()  { echo -e "${R}[X]${N} $*"; }
info() { echo -e "${B}[*]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }

[[ "$EUID" -ne 0 ]] && { err "با root اجرا کن."; exit 1; }

pause() { echo; read -rp "Enter برای برگشت به منو..." _; }


# ---------------------------------------------------------
# helpers
# ---------------------------------------------------------

ensure_packages() {
    info "نصب پکیج‌های سیستم..."
    export DEBIAN_FRONTEND=noninteractive

    # A previously interrupted apt/dpkg run leaves the package DB half-configured
    # and blocks every future install. Repair it before doing anything.
    if ! dpkg --configure -a 2>/dev/null; then
        warn "ترمیم dpkg..."
        dpkg --configure -a || true
    fi

    apt-get update -qq || { err "apt update نشد - اینترنت سرور رو چک کن"; return 1; }

    # Heal any previously-broken/half-installed packages (missing font deps,
    # g++, etc.) that block every new install.
    apt-get --fix-broken install -y 2>/dev/null || apt-get --fix-broken install -y || true

    # python3-venv is versioned on some releases (python3.10-venv etc.); install
    # the exact one that matches the running interpreter, plus the generic name.
    local pyver
    pyver=$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
    apt-get install -y -qq \
        python3 python3-pip python3-venv "python${pyver}-venv" \
        ffmpeg git unzip curl ca-certificates libchromaprint-tools aria2 2>/dev/null \
      || apt-get install -y python3 python3-pip "python${pyver}-venv" ffmpeg git unzip curl ca-certificates libchromaprint-tools aria2 \
      || warn "بعضی پکیج‌ها نصب نشدن - ادامه می‌دم و venv رو چک می‌کنم"

    # Verify venv actually works now, otherwise stop before we build a broken one.
    if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
        err "ماژول venv هنوز کار نمی‌کنه. دستی بزن:  apt install -y python${pyver}-venv"
        return 1
    fi
    ok "پکیج‌ها نصب شدن"
}

# `command -v deno` only says a file is on PATH. A deno that cannot execute -
# a truncated download, the wrong architecture, a lost execute bit, a glibc it
# was not built against - passes that check and then prints nothing, which is
# exactly what this reported:
#
#     [OK] Deno از قبل هست:
#
# yt-dlp needs deno to solve YouTube's JS challenge. No deno means no
# signature, which means no playable formats, on every player client at once:
#
#     ERROR: [youtube] Mc6voRYTu1c: Requested format is not available
#
# So the version string is the test. Producing it is the only proof that the
# thing runs, and it was already being printed - just never checked.
deno_version() {
    deno --version 2>/dev/null | head -1
}

ensure_deno() {
    local ver
    ver=$(deno_version)
    if [[ -n "$ver" ]]; then
        ok "Deno از قبل هست: $ver"
        return 0
    fi

    if command -v deno &>/dev/null; then
        warn "Deno هست ولی اجرا نمی‌شه - دوباره نصبش می‌کنم"
        warn "خطاش: $(deno --version 2>&1 | head -2 | tr '\n' ' ')"
    fi

    info "نصب Deno (برای yt-dlp لازمه)..."
    if ! curl -fsSL -o /tmp/deno.zip \
        https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
    then
        err "دانلود Deno نشد - دسترسی سرور به گیت‌هاب رو چک کن"
        return 1
    fi
    unzip -oq /tmp/deno.zip -d /usr/local/bin || { err "باز کردن فایل Deno نشد"; return 1; }
    chmod +x /usr/local/bin/deno
    rm -f /tmp/deno.zip

    ver=$(deno_version)
    if [[ -z "$ver" ]]; then
        err "Deno نصب شد ولی هنوز اجرا نمی‌شه:"
        deno --version 2>&1 | head -3
        err "تا این درست نشه، یوتیوب «Requested format is not available» می‌ده"
        return 1
    fi
    ok "Deno نصب شد: $ver"
}

ensure_user() {
    id -u "$BOT_USER" &>/dev/null || \
        useradd --system --create-home --shell /usr/sbin/nologin "$BOT_USER"
    ok "کاربر $BOT_USER آماده‌ست"
}

ensure_venv() {
    if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
        info "ساخت venv..."
        # A half-created .venv (e.g. from a failed python3-venv) must be wiped
        # first or python refuses to recreate it.
        rm -rf "$PROJECT_DIR/.venv"
        if ! sudo -u "$BOT_USER" python3 -m venv "$PROJECT_DIR/.venv"; then
            err "ساخت venv شکست خورد - python3-venv نصب نیست"
            return 1
        fi
    fi
    if [[ ! -x "$PROJECT_DIR/.venv/bin/pip" ]]; then
        err "venv خرابه (pip نیست). پوشه .venv رو پاک کن و دوباره نصب بزن."
        return 1
    fi
    info "نصب/آپدیت پکیج‌های پایتون (چند دقیقه طول می‌کشه)..."
    sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --upgrade pip setuptools wheel
    # Not quiet: this is the step most likely to fail, and the output is the
    # only useful diagnostic when it does.
    if ! sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install \
            --progress-bar off -r "$PROJECT_DIR/requirements.txt"; then
        err "نصب پکیج‌های پایتون شکست خورد - خطای بالا رو بفرست"
        return 1
    fi
    # Optional, and installed separately so a failure cannot take the whole
    # install with it. curl_cffi reproduces Chrome's TLS handshake, which is
    # what modules/ig_web.py uses to stop the header and the handshake telling
    # different stories. Absent, that module falls back to httpx unchanged.
    if sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q             --progress-bar off curl_cffi 2>/dev/null; then
        ok "curl_cffi نصب شد - اثر انگشت TLS شبیه کروم می‌شه"
    else
        warn "curl_cffi نصب نشد - بدونش هم کار می‌کنه"
    fi

    ok "پکیج‌های پایتون آماده‌ن"
}

ensure_service() {
    cp "$PROJECT_DIR/deploy/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload
    systemctl enable -q "$SERVICE_NAME"
    ok "سرویس systemd نصب شد"
}

fix_perms() {
    mkdir -p "$PROJECT_DIR/downloads"
    chown -R "$BOT_USER:$BOT_USER" "$PROJECT_DIR"
    # git reset --hard restores the mode recorded in the repo; keep this as a
    # safety net for clones made before the exec bit was committed.
    chmod +x "$PROJECT_DIR"/deploy/*.sh 2>/dev/null
    [[ -f "$PROJECT_DIR/.env" ]] && chmod 600 "$PROJECT_DIR/.env"
    git config --global --add safe.directory "$PROJECT_DIR" 2>/dev/null
}

syntax_check() {
    "$PROJECT_DIR/.venv/bin/python" -m py_compile \
        "$PROJECT_DIR"/main.py "$PROJECT_DIR"/config.py \
        "$PROJECT_DIR"/modules/*.py "$PROJECT_DIR"/handlers/*.py \
        "$PROJECT_DIR"/utils/*.py "$PROJECT_DIR"/web/*.py
}

set_env() {
    # Upsert a key in .env. Done in python rather than sed because these
    # values are URLs and secrets: a sed delimiter that happens to appear in
    # the value, or a bare & in a replacement, silently writes the wrong
    # thing - and a mangled app secret fails as "bad signature" much later.
    local key="$1" val="$2"
    python3 - "$PROJECT_DIR/.env" "$key" "$val" <<'PY'
import sys
from pathlib import Path

path, key, value = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
for i, line in enumerate(lines):
    if line.startswith(f"{key}="):
        lines[i] = f"{key}={value}"
        break
else:
    lines.append(f"{key}={value}")
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

get_env() {
    grep -E "^$1=" "$PROJECT_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-
}

is_git_repo() { [[ -d "$PROJECT_DIR/.git" ]]; }


# ---------------------------------------------------------
# 1) install / migrate
# ---------------------------------------------------------

do_install() {
    echo; info "=== نصب / اتصال به گیت‌هاب ==="; echo

    ensure_packages
    ensure_deno
    ensure_user

    if is_git_repo; then
        ok "پوشه از قبل به گیت وصله - سینک با گیت‌هاب"
        git config --global --add safe.directory "$PROJECT_DIR" 2>/dev/null
        git -C "$PROJECT_DIR" remote set-url origin "$REPO_URL" 2>/dev/null \
            || git -C "$PROJECT_DIR" remote add origin "$REPO_URL"
        if git -C "$PROJECT_DIR" fetch -q origin; then
            git -C "$PROJECT_DIR" reset -q --hard origin/main
            git -C "$PROJECT_DIR" clean -qfd
            ok "کد با آخرین نسخه همگام شد"
        else
            warn "fetch نشد - با کد فعلی ادامه می‌دم"
        fi

    elif [[ -d "$PROJECT_DIR" ]]; then
        warn "پوشه $PROJECT_DIR هست ولی به گیت وصل نیست."
        warn "فایل‌های کد با نسخه گیت‌هاب جایگزین می‌شن."
        warn ".env و .venv و downloads دست‌نخورده می‌مونن."
        echo
        read -rp "ادامه بدم؟ (y/n) " a
        [[ "$a" != "y" ]] && { warn "لغو شد"; return; }

        # Safety copy of the only irreplaceable file.
        if [[ -f "$PROJECT_DIR/.env" ]]; then
            local bak="/root/env-backup-$(date +%Y%m%d-%H%M%S)"
            cp "$PROJECT_DIR/.env" "$bak"
            ok "بکاپ .env: $bak"
        fi

        info "وصل کردن پوشه موجود به ریپو..."
        git config --global --add safe.directory "$PROJECT_DIR" 2>/dev/null
        git -C "$PROJECT_DIR" init -q -b main
        git -C "$PROJECT_DIR" remote remove origin 2>/dev/null
        git -C "$PROJECT_DIR" remote add origin "$REPO_URL"
        if ! git -C "$PROJECT_DIR" fetch -q origin; then
            err "fetch نشد - اینترنت سرور رو چک کن"
            return 1
        fi
        # Only tracked files are replaced; .env/.venv/downloads are ignored
        # by .gitignore, so reset and clean never touch them.
        git -C "$PROJECT_DIR" reset -q --hard origin/main
        git -C "$PROJECT_DIR" clean -qfd
        ok "کد با نسخه گیت‌هاب همگام شد"

    else
        info "کلون کردن ریپو..."
        if ! git clone -q "$REPO_URL" "$PROJECT_DIR"; then
            err "کلون نشد - اینترنت سرور رو چک کن"
            return 1
        fi
        ok "کلون شد"
    fi

    # After a reset the clone is empty of user files - put them back.
    if [[ -n "$RESTORE_DIR" && -d "$RESTORE_DIR" ]]; then
        cp "$RESTORE_DIR/.env" "$PROJECT_DIR/.env" 2>/dev/null && ok ".env برگردونده شد"
        cp "$RESTORE_DIR"/*cookies*.txt "$PROJECT_DIR/" 2>/dev/null && ok "کوکی‌ها برگردونده شدن"
    fi

    [[ -f "$PROJECT_DIR/.env" ]] || cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"

    # The bot exits immediately without a token, so never finish an install
    # with an empty one - that is the single most common "it won't start".
    if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+' "$PROJECT_DIR/.env"; then
        echo
        warn "توکن بات تو .env خالیه - بات بدون اون بالا نمیاد."
        read -rp "توکن رو از @BotFather بگیر و اینجا بزن: " token
        if [[ -n "$token" ]]; then
            if grep -q '^TELEGRAM_BOT_TOKEN=' "$PROJECT_DIR/.env"; then
                sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$token|" "$PROJECT_DIR/.env"
            else
                echo "TELEGRAM_BOT_TOKEN=$token" >> "$PROJECT_DIR/.env"
            fi
            ok "توکن ذخیره شد"
        else
            warn "خالی موند - بعدا با گزینه ۷ واردش کن"
        fi
    fi

    fix_perms
    ensure_venv || { err "نصب متوقف شد - venv آماده نشد. خطای بالا رو ببین."; return 1; }
    ensure_service

    info "چک سینتکس..."
    if ! syntax_check; then
        err "کد مشکل سینتکس داره - سرویس رو استارت نکردم"
        return 1
    fi
    ok "سینتکس سالمه"

    install_shortcut
    systemctl restart "$SERVICE_NAME"
    sleep 3
    echo
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "بات بالا اومد و داره کار می‌کنه"
        echo; ok "نصب تموم شد. از این به بعد فقط 'botctl' رو بزن."
        warn "اگه 'botctl' رو پیدا نکرد، یه بار این رو بزن:  hash -r"
    else
        err "بات بالا نیومد. خطای واقعی:"
        echo
        journalctl -u "$SERVICE_NAME" --no-pager -n 25
    fi
}


# ---------------------------------------------------------
# reset - wipe everything and reinstall from scratch
# ---------------------------------------------------------

do_reset() {
    echo; warn "=== نصب مجدد از صفر ==="
    warn "کل $PROJECT_DIR پاک می‌شه (کد، venv، دانلودها) و از نو کلون می‌شه."
    warn ".env و کوکی‌ها بکاپ گرفته و برگردونده می‌شن."
    echo
    read -rp "برای ادامه بنویس yes: " a
    if [[ "$a" != "yes" ]]; then
        warn "لغو شد - هیچی پاک نشد"
        return
    fi

    if [[ -d "$PROJECT_DIR" ]]; then
        RESTORE_DIR="/root/bot-backup-$(date +%Y%m%d-%H%M%S)"
        mkdir -p "$RESTORE_DIR"
        cp "$PROJECT_DIR/.env" "$RESTORE_DIR/" 2>/dev/null
        cp "$PROJECT_DIR"/*cookies*.txt "$RESTORE_DIR/" 2>/dev/null
        ok "بکاپ گرفته شد: $RESTORE_DIR"
    fi

    info "توقف و حذف سرویس..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null
    systemctl disable -q "$SERVICE_NAME" 2>/dev/null
    rm -f "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload
    systemctl reset-failed "$SERVICE_NAME" 2>/dev/null

    info "پاک کردن $PROJECT_DIR ..."
    rm -rf "$PROJECT_DIR"
    ok "پاک شد"

    # do_install now takes the clean-clone path and restores from RESTORE_DIR.
    do_install
}


# ---------------------------------------------------------
# 2) update
# ---------------------------------------------------------

do_update() {
    echo; info "=== آپدیت از گیت‌هاب ==="; echo

    if ! is_git_repo; then
        err "پوشه به گیت وصل نیست. اول گزینه ۱ رو بزن."
        return 1
    fi

    local before
    before=$(git -C "$PROJECT_DIR" rev-parse --short HEAD)

    info "گرفتن آخرین تغییرات..."
    if ! git -C "$PROJECT_DIR" fetch -q origin; then
        err "fetch نشد - اینترنت سرور رو چک کن"
        return 1
    fi

    local after
    after=$(git -C "$PROJECT_DIR" rev-parse --short origin/main)

    if [[ "$before" == "$after" ]]; then
        ok "همین الان آخرین نسخه‌ست ($before) - چیزی برای آپدیت نیست"
        # YouTube breaking has nothing to do with whether we have new commits,
        # so the extractor is refreshed on this path too. Putting it only
        # after the pull meant "already up to date" also meant "and yt-dlp
        # stays as stale as it was".
        refresh_ytdlp
        systemctl restart "$SERVICE_NAME"
        ok "ریستارت شد"
        return
    fi

    echo; info "تغییرات $before تا $after:"
    git -C "$PROJECT_DIR" log --oneline "$before..origin/main" | head -20
    echo

    git -C "$PROJECT_DIR" reset -q --hard origin/main
    git -C "$PROJECT_DIR" clean -qfd
    ok "کد آپدیت شد"

    fix_perms
    ensure_venv

    refresh_ytdlp

    info "چک سینتکس..."
    if ! syntax_check; then
        err "کد جدید مشکل سینتکس داره - برمی‌گردم به $before"
        git -C "$PROJECT_DIR" reset -q --hard "$before"
        fix_perms
        systemctl restart "$SERVICE_NAME"
        return 1
    fi
    ok "سینتکس سالمه"

    systemctl restart "$SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "بات با نسخه جدید بالا اومد"
    else
        err "بات بالا نیومد - لاگ:"
        journalctl -u "$SERVICE_NAME" --no-pager -n 25
    fi
}


# ---------------------------------------------------------
# other actions
# ---------------------------------------------------------

do_status() {
    echo
    systemctl --no-pager --lines=0 status "$SERVICE_NAME" 2>/dev/null || err "سرویس نصب نیست"
    echo
    if is_git_repo; then
        info "نسخه فعلی: $(git -C "$PROJECT_DIR" log -1 --format='%h - %s (%cr)')"
    fi
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        echo
        info "تنظیمات .env:"
        local t ig ba
        t=$(grep -E '^TELEGRAM_BOT_TOKEN=.+' "$PROJECT_DIR/.env" | head -1)
        ig=$(grep -E '^IG_SESSIONID=.+' "$PROJECT_DIR/.env" | head -1)
        ba=$(grep -E '^BOT_API_BASE_URL=.+' "$PROJECT_DIR/.env" | head -1)
        [[ -n "$t"  ]] && ok "توکن تلگرام: ست شده" || err "توکن تلگرام: خالی!"
        [[ -n "$ig" ]] && ok "کوکی اینستا: ست شده (استوری فعاله)" \
                       || info "کوکی اینستا: خالی (ریلز کار می‌کنه، استوری نه)"
        [[ -n "$ba" ]] && ok "Local Bot API: فعال (آپلود تا ۲ گیگ)" \
                       || info "Local Bot API: غیرفعال (سقف آپلود ۵۰ مگ)"
    fi
    echo
    # Same blind spot as ensure_deno had: a deno on PATH that does not run
    # reported as present, and yt-dlp cannot touch YouTube without it.
    _dv=$(deno_version)
    [[ -n "$_dv" ]] && ok "Deno: $_dv" \
                    || err "Deno نیست یا اجرا نمی‌شه - یوتیوب بدون این کار نمی‌کنه (botctl ytdlp)"
    [[ -x "$PROJECT_DIR/.venv/bin/python" ]] && \
        ok "yt-dlp: $("$PROJECT_DIR/.venv/bin/python" -m yt_dlp --version 2>/dev/null || echo '?')"
}

# `botctl logs | grep ...` looks like it should work and cannot: -f follows,
# so the pipe blocks, and -n 40 means the history being searched is forty
# lines deep. Two separate diagnostics came back empty that way and were read
# as "the thing never happened" rather than "the log was never searched".
do_logs() {
    local n="${1:-}"
    if [[ -n "$n" ]]; then
        journalctl -u "$SERVICE_NAME" --no-pager -n "$n"
        return
    fi
    echo; info "لاگ زنده - برای خروج Ctrl+C بزن"
    info "برای گشتن تو تاریخچه:  botctl logs 2000   یا   botctl find <الگو>"; echo
    journalctl -u "$SERVICE_NAME" -f -n 40
}

do_find() {
    local pattern="${1:-}"
    local lines="${2:-5000}"
    if [[ -z "$pattern" ]]; then
        err "الگو لازمه. مثال:  botctl find 'yt-dlp client'"
        return 1
    fi
    # grep -iE is an EXTENDED regex: | is already alternation there, and
    # \| means a literal pipe. Anyone who has used grep writes \| out of
    # habit, and it then silently matches nothing - which reads as "the bot
    # never logged that" rather than "your pattern was wrong".
    pattern=$(printf '%s' "$pattern" | sed 's/\\\\|/|/g')

    info "گشتن دنبال «$pattern» تو $lines خط آخر:"
    journalctl -u "$SERVICE_NAME" --no-pager -n "$lines" \
        | grep -iE "$pattern" || warn "چیزی پیدا نشد تو $lines خط آخر"
}

do_spotify() {
    echo; info "=== کلید اسپاتیفای (فقط برای پلی‌لیست‌های بزرگ) ==="
    warn "بدون کلید، اسپاتیفای فقط ۱۰۰ ترک اول رو می‌ده."
    warn "با کلید، پلی‌لیست‌های چند هزارتایی کامل خونده می‌شن."
    echo
    info "۱) برو developer.spotify.com/dashboard و وارد شو"
    info "۲) Create app بزن:"
    info "     - App name / description: هرچی خواستی (کلمه Spotify نباشه)"
    info "     - Redirect URI:  http://127.0.0.1:8080/callback   بعد Add رو بزن"
    info "       (localhost رو قبول نمی‌کنه؛ باید https باشه یا آی‌پی عددی)"
    info "     - تیک Web API رو بزن، شرایط رو قبول کن، Save"
    info "۳) تو Settings اپ: Client ID و View client secret"
    warn "این Redirect URI هیچ‌وقت استفاده نمی‌شه، فقط فرم اجبارش می‌کنه."
    echo
    read -rp "Client ID: " cid
    read -rp "Client Secret: " csec
    if [[ -z "$cid" || -z "$csec" ]]; then
        warn "خالی بود، لغو شد"
        return
    fi

    local f="$PROJECT_DIR/.env"
    for pair in "SPOTIFY_CLIENT_ID=$cid" "SPOTIFY_CLIENT_SECRET=$csec"; do
        local k="${pair%%=*}"
        if grep -q "^$k=" "$f"; then
            sed -i "s|^$k=.*|$pair|" "$f"
        else
            echo "$pair" >> "$f"
        fi
    done
    systemctl restart "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
        && ok "ذخیره شد - حالا پلی‌لیست‌های بزرگ کامل خونده می‌شن" \
        || err "بات بالا نیومد - گزینه ۶ رو ببین"
}


do_channel() {
    echo; info "=== کانال اجباری (قفل عضویت) ==="
    warn "کاربر تا تو این کانال عضو نشه، بات جواب نمی‌ده."
    warn "مهم: بات باید ادمین اون کانال باشه وگرنه عضویت رو نمی‌تونه چک کنه."
    echo
    info "برای غیرفعال کردن، خالی بذار و Enter بزن."
    read -rp "آیدی کانال (@name یا لینک): " ch

    local f="$PROJECT_DIR/.env"
    if grep -q '^REQUIRED_CHANNEL=' "$f"; then
        sed -i "s|^REQUIRED_CHANNEL=.*|REQUIRED_CHANNEL=$ch|" "$f"
    else
        echo "REQUIRED_CHANNEL=$ch" >> "$f"
    fi
    systemctl restart "$SERVICE_NAME"
    sleep 2
    if [[ -z "$ch" ]]; then
        ok "قفل کانال غیرفعال شد"
    else
        ok "قفل روی $ch فعال شد"
        warn "یادت نره بات رو تو اون کانال ادمین کنی."
    fi
}


do_igcheck() {
    echo; info "=== وضعیت اینستاگرام ==="; echo
    local f="$PROJECT_DIR/.env"
    if grep -qE '^IG_SESSIONID=.+' "$f"; then
        ok "کوکی ست شده"
        grep -E '^(INSTAGRAM_USERNAME|IG_DS_USER_ID)=' "$f" | sed 's/^/    /'
    else
        err "کوکی ست نشده"
        warn "اینستاگرام دسترسی بدون اکانت رو تقریبا بسته؛ بدون کوکی اکثر پست‌ها نمیاد."
        warn "با گزینه ۱۰ کوکی‌ها رو ست کن."
    fi
    echo
    info "برای تست واقعی سشن، تو خود بات بزن:  /igcheck"
    echo
    info "آخرین خطاهای اینستاگرام:"
    journalctl -u "$SERVICE_NAME" --no-pager -n 400         | grep -iE "instagram|instaloader" | tail -12
}


do_engines() {
    echo; info "=== موتورهای تشخیص آهنگ ==="
    info "شزم همیشه اول اجرا می‌شه، رایگان و بدون سقف."
    info "بقیه فقط وقتی شزم چیزی پیدا نکرد امتحان می‌شن."
    echo
    info "AcoustID: رایگان و عملا بی‌نهایت — کلیدشو از اینجا بگیر:"
    info "   https://acoustid.org/new-application"
    info "AudD: دقیق‌تر ولی سهمیه رایگانش کمه (اختیاری) — audd.io"
    echo
    read -rp "AcoustID API key (خالی = رد کن): " ak
    read -rp "AudD API token   (خالی = رد کن): " at

    local f="$PROJECT_DIR/.env"
    _set_env() {
        local k="$1" v="$2"
        [[ -z "$v" ]] && return
        if grep -q "^$k=" "$f"; then sed -i "s|^$k=.*|$k=$v|" "$f"; else echo "$k=$v" >> "$f"; fi
    }
    _set_env ACOUSTID_API_KEY "$ak"
    _set_env AUDD_API_TOKEN "$at"

    if ! command -v fpcalc &>/dev/null; then
        info "نصب fpcalc (برای AcoustID لازمه)..."
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libchromaprint-tools &>/dev/null
    fi
    command -v fpcalc &>/dev/null && ok "fpcalc آماده‌ست" || warn "fpcalc نصب نشد - AcoustID کار نمی‌کنه"

    systemctl restart "$SERVICE_NAME"
    sleep 2
    ok "ذخیره شد. تو بات با /recstatus وضعیت رو ببین."
}


do_reclog() {
    echo; info "=== لاگ تشخیص آهنگ (آخرین تلاش‌ها) ==="; echo
    journalctl -u "$SERVICE_NAME" --no-pager -n 500 \
        | grep -iE "recognize:|Shazam|get_file" | tail -40
    echo
    info "اگه خالیه: یه ویدیو به بات بفرست و دوباره این رو بزن."
}


do_errors() {
    echo; info "=== ۴۰ خطای آخر ==="; echo
    journalctl -u "$SERVICE_NAME" --no-pager -n 400 \
        | grep -iE "error|traceback|exception|failed|❌" | tail -40
}

do_env() {
    "${EDITOR:-nano}" "$PROJECT_DIR/.env"
    chmod 600 "$PROJECT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env"
    read -rp "ریستارت کنم که اعمال شه؟ (y/n) " a
    [[ "$a" == "y" ]] && { systemctl restart "$SERVICE_NAME"; sleep 2; ok "ریستارت شد"; }
}

# yt-dlp is deliberately unpinned - requirements.txt says "keep latest; sites
# break often" - but pip leaves an already-installed unpinned package exactly
# where it is, so nothing moved it unless somebody went looking through the
# menu. YouTube rots quietly in the meantime, and the symptom does not name
# its cause:
#     ERROR: [youtube] Mc6voRYTu1c: Requested format is not available
refresh_ytdlp() {
    info "آپدیت yt-dlp (یوتیوب مرتب عوض می‌شه)..."
    if sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --upgrade yt-dlp; then
        ok "yt-dlp: $("$PROJECT_DIR/.venv/bin/python" -m yt_dlp --version 2>/dev/null || echo '?')"
    else
        warn "آپدیت yt-dlp نشد - با نسخه فعلی ادامه می‌دم"
    fi
    ensure_deno
}

# "Sign in to confirm you're not a bot" is not a yt-dlp problem and no client
# in the ladder gets around it any more - it is YouTube declining to serve a
# datacenter address anonymously. yt-dlp's own answer is a cookie jar, and
# modules/youtube.py already reads one and reorders the ladder when it exists.
# There was simply no way to put one there.
# A browser cookie is a WEB credential. realtime speaks to the MOBILE api and
# has therefore never had one it could use, which is why it has been refused
# on every account so far while the web poller worked from the same cookie.
# This creates the credential realtime actually wants - and it outlives a
# browser session by months rather than hours.
do_igweblogin() {
    echo; info "=== لاگین دسکتاپ اینستاگرام (یوزر/پس) ==="; echo
    echo "این با api وب لاگین می‌کنه - همون دری که تو مرورگر باز می‌کنی."
    echo "چک‌پوینتش کددار هست، پس اگه تایید خواست کد به ایمیلت میاد."
    echo
    warn "با اکانت اصلیت این کارو نکن."
    echo
    read -rp "ادامه بدم؟ (y/n) " a
    [[ "$a" != "y" ]] && return 0

    # The script writes the cookies here rather than printing them: they are
    # credentials, and a terminal scrollback is not where those belong.
    local jar
    jar=$(mktemp)
    chmod 600 "$jar"
    chown "$BOT_USER" "$jar"

    if ! run_py "$PROJECT_DIR/deploy/igweblogin.py" "$jar"; then
        rm -f "$jar"
        err "لاگین نشد"
        return 1
    fi

    if [[ ! -s "$jar" ]]; then
        rm -f "$jar"
        err "چیزی برای ذخیره برنگشت"
        return 1
    fi

    local key value
    while IFS='=' read -r key value; do
        [[ -z "$key" ]] && continue
        set_env "$key" "$value"
    done < "$jar"
    rm -f "$jar"
    ok "کوکی‌ها تو .env ذخیره شدن"

    # The saved jar holds whatever Instagram rotated the OLD session to. Left
    # in place it overrides the one just minted.
    rm -f "$PROJECT_DIR/downloads/ig_web_cookies.json"
    ok "کوکی‌جار قبلی پاک شد"

    systemctl restart "$SERVICE_NAME"
    ok "ریستارت شد"
    echo
    info "چک کن:  botctl find 'ig web' 200"
}


do_admin() {
    local want="${1:-}"
    local cur
    cur=$(get_env ADMIN_IDS)

    echo; info "=== ادمین‌های بات ==="; echo
    if [[ -n "$cur" ]]; then
        echo "الان: $cur"
    else
        warn "هیچ ادمینی تنظیم نشده - پنل و ارسال همگانی برای هیچ‌کس کار نمی‌کنه."
    fi
    echo

    if [[ -z "$want" ]]; then
        echo "آیدی عددیت رو از خود بات بگیر: تو تلگرام /admin بزن،"
        echo "بات آیدیت رو بهت می‌گه."
        echo
        read -rp "آیدی عددی (خالی = انصراف): " want
        [[ -z "$want" ]] && return 0
    fi

    if [[ ! "$want" =~ ^[0-9]+$ ]]; then
        err "آیدی باید فقط عدد باشه: $want"
        return 1
    fi

    # Added to the list rather than replacing it: a second admin must not
    # silently remove the first, which is how somebody locks themselves out.
    if [[ ",$cur," == *",$want,"* ]]; then
        ok "این آیدی از قبل ادمینه"
        return 0
    fi
    if [[ -n "$cur" ]]; then
        set_env ADMIN_IDS "$cur,$want"
    else
        set_env ADMIN_IDS "$want"
    fi
    ok "اضافه شد: $want"

    systemctl restart "$SERVICE_NAME"
    ok "ریستارت شد - تو تلگرام /admin بزن"
}


do_iglogin() {
    echo; info "=== لاگین موبایل اینستاگرام (سشن ماندگار) ==="; echo
    warn "با اکانت اصلیت این کارو نکن."
    echo
    echo "قبل از این باید اکانت آماده باشه:"
    echo "  • روی گوشی واقعی و اپ رسمی ساخته شده باشه"
    echo "  • چند روز عادی ازش استفاده شده باشه (فالو، لایک، یه پست)"
    echo "  • 2FA خاموش باشه"
    echo "  • تو اپ گوشی همچنان لاگین بمونه - خارج نشو"
    echo
    read -rp "آماده‌ست؟ (y/n) " a
    [[ "$a" != "y" ]] && return 0

    if ! run_py -c "import instagrapi" 2>/dev/null; then
        info "نصب instagrapi..."
        sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --progress-bar off instagrapi \
            || { err "نصب نشد"; return 1; }
    fi

    # As the bot user: the session file it writes has to be readable by the
    # service, and a root-owned one silently is not.
    run_py "$PROJECT_DIR/deploy/iglogin.py" ${1:+"$1"} || return 1

    systemctl restart "$SERVICE_NAME"
    ok "ریستارت شد"
    echo
    info "چک کن:  botctl find 'ig mqtt' 500"
}

do_ytcookies() {
    local jar="$PROJECT_DIR/youtube_cookies.txt"

    echo; info "=== کوکی یوتیوب ==="; echo
    warn "این کوکی یعنی دسترسی کامل به اون اکانت گوگل."
    warn "با اکانت اصلیت این کارو نکن. یه اکانت گوگل یه‌بارمصرف بساز،"
    warn "با همون تو یوتیوب لاگین کن، و کوکی اونو بذار."
    echo
    echo "روی کامپیوتر خودت:"
    echo "  ۱. با اکانت یه‌بارمصرف تو مرورگر وارد یوتیوب شو"
    echo "  ۲. یه افزونه‌ی «Get cookies.txt» نصب کن (فرمت Netscape)"
    echo "  ۳. روی youtube.com فایل رو export کن"
    echo "  ۴. محتواشو کپی کن"
    echo
    read -rp "آماده‌ای؟ (y/n) " a
    [[ "$a" != "y" ]] && return 0

    echo
    info "حالا محتوای فایل رو پیست کن، بعد Enter و Ctrl+D بزن:"
    local tmp
    tmp=$(mktemp)
    cat > "$tmp"

    if [[ ! -s "$tmp" ]]; then
        err "چیزی پیست نشد"; rm -f "$tmp"; return 1
    fi
    # A cookie jar that yt-dlp cannot parse fails at download time with a
    # message about formats, which is the same message this is meant to cure.
    if ! grep -qi '^\.\?youtube\.com' "$tmp"; then
        err "این فایل کوکی یوتیوب نیست - هیچ خطی برای youtube.com توش نیست"
        rm -f "$tmp"; return 1
    fi

    mv "$tmp" "$jar"
    chmod 600 "$jar"
    chown "$BOT_USER:$BOT_USER" "$jar"
    set_env YT_COOKIES_FILE "$jar"
    ok "کوکی ذخیره شد ($(grep -c . "$jar") خط)"

    echo; info "تست با یه ویدیو..."
    if run_py -m yt_dlp \
        --cookies "$jar" --skip-download --print '%(title)s' \
        'https://www.youtube.com/watch?v=jNQXAC9IVRw' 2>&1 | tail -3
    then
        ok "یوتیوب با این کوکی جواب داد"
    else
        warn "تست جواب نداد - ممکنه کوکی منقضی باشه یا اکانت تایید بخواد"
    fi

    systemctl restart "$SERVICE_NAME"
    ok "ریستارت شد. حالا تو لاگ باید ببینی client 'default' سریع جواب می‌ده:"
    echo "    botctl find 'yt-dlp client'"
}

do_ytdlp() {
    echo; info "آپدیت yt-dlp (وقتی یوتیوب خراب می‌شه اینو بزن)..."
    refresh_ytdlp
    systemctl restart "$SERVICE_NAME"
    ok "ریستارت شد"
}

do_botapi() {
    echo; info "=== Local Bot API - آپلود تا ۲ گیگ ==="; echo
    warn "اول باید از my.telegram.org مقدار API ID و API HASH بگیری."
    echo
    read -rp "API ID: " api_id
    read -rp "API HASH: " api_hash
    [[ -z "$api_id" || -z "$api_hash" ]] && { err "خالی بود، لغو شد"; return; }

    if ! command -v docker &>/dev/null; then
        info "نصب docker..."
        apt-get install -y -qq docker.io
        systemctl enable -q --now docker
    fi

    local token
    token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$PROJECT_DIR/.env" | cut -d= -f2-)
    if [[ -n "$token" ]]; then
        info "logout از سرور کلود تلگرام (لازمه وگرنه لوکال وصل نمی‌شه)..."
        curl -s "https://api.telegram.org/bot$token/logOut" >/dev/null
        sleep 2
    fi

    # In --local mode getFile answers with a path on the SERVER's filesystem
    # rather than a download URL. With a named docker volume that path exists
    # only inside the container, so the bot could not open it and every media
    # download failed with "Not Found". Bind-mount the same path on the host.
    _botapi_run "$api_id" "$api_hash" || return 1

    if grep -q '^BOT_API_BASE_URL=' "$PROJECT_DIR/.env"; then
        sed -i 's|^BOT_API_BASE_URL=.*|BOT_API_BASE_URL=http://127.0.0.1:8081|' "$PROJECT_DIR/.env"
    else
        echo "BOT_API_BASE_URL=http://127.0.0.1:8081" >> "$PROJECT_DIR/.env"
    fi
    if grep -q '^MAX_UPLOAD_MB=' "$PROJECT_DIR/.env"; then
        sed -i 's|^MAX_UPLOAD_MB=.*|MAX_UPLOAD_MB=2000|' "$PROJECT_DIR/.env"
    else
        echo "MAX_UPLOAD_MB=2000" >> "$PROJECT_DIR/.env"
    fi

    sleep 5
    systemctl restart "$SERVICE_NAME"
    ok "فعال شد - آپلود تا ۲ گیگ و دانلود فایل‌های بزرگ"
    info "پوشه داده: $data_dir (باید برای کاربر $BOT_USER خوندنی باشه)"
}

do_instagram() {
    echo; info "=== کوکی اینستاگرام (فقط برای استوری و پست چندعکسی) ==="; echo
    warn "با یه اکانت یه‌بارمصرف تو مرورگر لاگین کن، بعد:"
    warn "F12 > Application > Cookies > instagram.com"
    echo
    read -rp "یوزرنیم اکانت: " u
    read -rp "sessionid: " s
    read -rp "csrftoken: " c
    read -rp "ds_user_id: " d
    [[ -z "$u" || -z "$s" ]] && { err "یوزرنیم و sessionid لازمه"; return; }

    sed -i "s|^INSTAGRAM_USERNAME=.*|INSTAGRAM_USERNAME=$u|" "$PROJECT_DIR/.env"
    sed -i "s|^IG_SESSIONID=.*|IG_SESSIONID=$s|"             "$PROJECT_DIR/.env"
    sed -i "s|^IG_CSRFTOKEN=.*|IG_CSRFTOKEN=$c|"             "$PROJECT_DIR/.env"
    sed -i "s|^IG_DS_USER_ID=.*|IG_DS_USER_ID=$d|"           "$PROJECT_DIR/.env"
    systemctl restart "$SERVICE_NAME"
    ok "ذخیره شد - استوری حالا فعاله"
}

_looks_like_ipv4() {
    [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

_server_ips() {
    # The interface first. A VPS carries its public address locally, and that
    # answer cannot be blocked, rate-limited, or - as actually happened here -
    # replaced by an HTML 403 page that then gets compared against a real IP.
    ip -4 -o addr show scope global 2>/dev/null | awk '{split($4, a, "/"); print a[1]}'

    # Only useful behind NAT, where the local address is private. Several
    # echo services, because any one of them can refuse a datacenter IP, and
    # every answer is validated before it is trusted.
    local url answer
    for url in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
        answer=$(curl -sf --max-time 6 "$url" 2>/dev/null | tr -d '[:space:]')
        if _looks_like_ipv4 "$answer"; then
            echo "$answer"
            return 0
        fi
    done
}

_resolve_a() {
    local out
    out=$(getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | head -1)
    [[ -z "$out" ]] && out=$(getent hosts "$1" 2>/dev/null | awk '{print $1}' | head -1)
    _looks_like_ipv4 "$out" && echo "$out"
}

ensure_caddy() {
    if command -v caddy &>/dev/null; then
        ok "Caddy از قبل نصبه"
        return 0
    fi
    info "نصب Caddy..."
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy || { err "نصب Caddy شکست خورد"; return 1; }
    ok "Caddy نصب شد"
}

_caddy_site() {
    # Written as its own file, never into the main Caddyfile: the box may
    # already be serving something else and clobbering that config would take
    # an unrelated site down.
    local domain="$1" port="$2"
    mkdir -p /etc/caddy/Caddyfile.d
    cat > /etc/caddy/Caddyfile.d/telegram-bot.caddy <<EOF
$domain {
    reverse_proxy 127.0.0.1:$port
}
EOF
    touch /etc/caddy/Caddyfile
    grep -q 'Caddyfile.d' /etc/caddy/Caddyfile \
        || echo 'import /etc/caddy/Caddyfile.d/*.caddy' >> /etc/caddy/Caddyfile

    if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile &>/dev/null; then
        err "کانفیگ Caddy معتبر نیست:"
        caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
        return 1
    fi
    systemctl enable -q --now caddy
    systemctl reload caddy || systemctl restart caddy
    ok "Caddy برای $domain تنظیم شد"
}

# The official path needs a verified Meta developer account, a domain and,
# for anyone who is not an app tester, App Review. That is a wall a lot of
# people cannot get over, so it is one option here rather than the only one.
_igdirect_official() {
    echo; info "--- دامنه و TLS ---"
    local domain
    read -rp "ساب‌دامنه (مثلا ig.example.com): " domain
    [[ -z "$domain" ]] && { err "دامنه لازمه - متا به IP خالی وب‌هوک نمی‌فرسته"; return 1; }

    local resolved ips candidate matched=0
    resolved=$(_resolve_a "$domain")
    ips=$(_server_ips)
    for candidate in $ips; do
        [[ "$candidate" == "$resolved" ]] && { matched=1; break; }
    done

    if [[ -z "$resolved" ]]; then
        warn "$domain اصلا resolve نمی‌شه. تا DNS پخش نشه Let's Encrypt سرتیفیکیت نمی‌ده."
        read -rp "بازم ادامه بدم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 1
    elif (( matched )); then
        ok "DNS درسته: $domain -> $resolved"
    elif [[ -z "$ips" ]]; then
        # Not a reason to stop: Caddy's own ACME challenge is the real test,
        # and this check failing says nothing about whether DNS is right.
        warn "IP این سرور رو نتونستم تشخیص بدم - از چک DNS رد می‌شم."
        ok "$domain -> $resolved"
    else
        warn "$domain به $resolved اشاره می‌کنه، ولی IP این سرور: $(echo $ips | tr '\n' ' ')"
        read -rp "بازم ادامه بدم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 1
    fi

    if ss -lntp 2>/dev/null | grep -qE ':(80|443)\s' && ! ss -lntp 2>/dev/null | grep -qE ':(80|443)\s.*caddy'; then
        warn "پورت ۸۰/۴۴۳ رو یه سرویس دیگه گرفته (nginx؟). Caddy نمی‌تونه سرتیفیکیت بگیره."
        ss -lntp | grep -E ':(80|443)\s'
        read -rp "بازم ادامه بدم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 1
    fi

    local port path
    port=$(get_env IG_WEBHOOK_PORT); port="${port:-8088}"
    path=$(get_env IG_WEBHOOK_PATH); path="${path:-/ig/webhook}"

    ensure_caddy || return 1
    _caddy_site "$domain" "$port" || return 1

    # --- Meta credentials ---
    echo; info "--- کلیدهای متا ---"
    warn "این‌ها رو من نمی‌بینم؛ مستقیم تو .env همین سرور ذخیره می‌شن."
    local app_id app_secret token verify
    read -rp "App ID: " app_id
    read -rsp "App Secret: " app_secret; echo
    read -rsp "Long-lived Access Token: " token; echo
    verify=$(get_env IG_VERIFY_TOKEN)
    if [[ -z "$verify" ]]; then
        verify=$(openssl rand -hex 16)
        info "Verify token ساخته شد: $verify"
    fi

    [[ -z "$app_secret" ]] && { err "App Secret لازمه - بدونش هر درخواستی رد می‌شه"; return 1; }
    [[ -z "$token" ]] && { err "Access Token لازمه"; return 1; }

    set_env IG_APP_ID        "$app_id"
    set_env IG_APP_SECRET    "$app_secret"
    set_env IG_ACCESS_TOKEN  "$token"
    set_env IG_VERIFY_TOKEN  "$verify"
    set_env IG_WEBHOOK_PORT  "$port"
    set_env IG_WEBHOOK_PATH  "$path"
    set_env IG_PUBLIC_URL    "https://$domain$path"
    set_env IG_WEBHOOK_HOST  "127.0.0.1"

    IGD_DOMAIN="$domain"
    IGD_PATH="$path"
    IGD_PORT="$port"
    IGD_VERIFY="$verify"
    return 0
}

_igdirect_standby() {
    echo; info "--- مسیر پشتیبان (instagrapi) ---"
    warn "این مسیر غیررسمیه و شرایط استفاده‌ی اینستاگرام رو نقض می‌کنه."
    warn "اکانت می‌تونه بدون هشدار و بدون امکان اعتراض بسته بشه."
    warn "حتما یه اکانت جدا بساز - نه اکانت اصلیت، نه اکانتی که توکن رسمی روشه."
    echo
    read -rp "ادامه بدم؟ (y/n) " a
    [[ "$a" != "y" ]] && return 1

    # Deliberately not in requirements.txt: instagrapi pins pydantic and
    # fights shazamio's resolver, so a bad day here must not be able to break
    # a working venv on every update.
    if ! sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install --progress-bar off instagrapi; then
        err "نصب instagrapi شکست خورد"
        return 1
    fi
    ok "instagrapi نصب شد"

    local dm_user
    read -rp "یوزرنیم اکانت دایرکت: " dm_user
    [[ -z "$dm_user" ]] && { err "یوزرنیم لازمه"; return 1; }
    set_env IG_DM_USERNAME "$dm_user"

    echo
    echo "  1) کوکی sessionid (پیشنهادی)"
    echo "     لاگین تو مرورگر خودت انجام می‌شه، سرور فقط نتیجه رو نگه می‌داره."
    echo "  2) پسورد"
    echo "     لاگین از خود سرور. اینستاگرام معمولا از IP دیتاسنتر ردش می‌کنه"
    echo "     با BadPassword — حتی وقتی پسورد درسته."
    echo
    read -rp "انتخاب: [1] " how
    how="${how:-1}"

    if [[ "$how" == "1" ]]; then
        echo
        # A cookie taken from a tab you keep using is fragile: Instagram
        # rotates the session, and the browser gets the new value while the
        # server keeps the old one. An incognito window closed without
        # logging out leaves the session alive and nothing rotating it.
        info "مهم - کوکی رو اینجوری بگیر که دووم بیاره:"
        echo "  ۱. یه پنجره‌ی ناشناس (Incognito) باز کن"
        echo "  ۲. با اکانت بات وارد instagram.com شو"
        echo "  ۳. F12 → Application → Cookies → instagram.com"
        echo "     سه تا کوکی: sessionid, csrftoken, ds_user_id"
        echo "  ۴. پنجره رو *بدون Log out* ببند"
        echo
        warn "تو همون مرورگر دوباره با این اکانت لاگین نکن - اینستاگرام سشن رو"
        warn "می‌چرخونه، مرورگر مقدار جدید رو می‌گیره و سرور با قدیمی می‌مونه."
        warn "این کوکی‌ها رو من نمی‌بینم؛ مستقیم تو .env همین سرور ذخیره می‌شن."
        # All three, because with them the WEB api can be used - and that is
        # the api this cookie belongs to. Given only sessionid we are back to
        # handing a browser cookie to the mobile api, which refuses it.
        local sid csrf dsid
        read -rsp "sessionid: " sid; echo
        [[ -z "$sid" ]] && { err "خالی بود"; return 1; }
        read -rp  "csrftoken: " csrf
        read -rp  "ds_user_id: " dsid
        set_env IG_DM_SESSIONID  "$sid"
        set_env IG_DM_CSRFTOKEN  "$csrf"
        set_env IG_DM_DS_USER_ID "$dsid"

        # A cookie and the browser that made it are one thing, not two.
        # Instagram ties the session to the client, so sending this cookie
        # with the bot's built-in Chrome/124 string is that session showing up
        # on a different machine - every single request. Sessions have been
        # dying within hours, which is what that looks like from the far side.
        echo
        info "آخرین تیکه: User-Agent همون مرورگر."
        echo "  تو همون پنجره‌ی ناشناس، تو Console بزن:  navigator.userAgent"
        echo "  یا برو به:  whatmyuseragent.com"
        warn "بدون این، کوکی با یه مرورگر دیگه فرستاده می‌شه و اینستاگرام"
        warn "سشن رو زودتر می‌بنده. این محتمل‌ترین دلیل عمر چندساعته‌ست."
        local ua
        read -rp "User-Agent: " ua
        if [[ -n "$ua" ]]; then
            set_env IG_DM_USER_AGENT "$ua"
            ok "UA ذخیره شد"
        else
            warn "خالی موند - از UA پیش‌فرض استفاده می‌شه (Chrome 124)"
        fi
        set_env IG_DM_PASSWORD ""

        # The saved jar holds the cookies Instagram rotated to from the OLD
        # session. Left in place it overrides the one just pasted, and the bot
        # keeps using a cookie that already stopped working.
        rm -f "$PROJECT_DIR/downloads/ig_web_cookies.json"
        ok "کوکی‌جار قبلی پاک شد"

        if [[ -n "$csrf" && -n "$dsid" ]]; then
            ok "هر سه کوکی ست شد - از API وب استفاده می‌شه (بدون instagrapi)"
        else
            warn "بدون csrftoken و ds_user_id فقط مسیر موبایل می‌مونه، که همون ۴۰۳ رو می‌ده."
        fi
    else
        local dm_pass
        read -rsp "پسورد اکانت دایرکت: " dm_pass; echo
        [[ -z "$dm_pass" ]] && { err "پسورد لازمه"; return 1; }
        set_env IG_DM_PASSWORD "$dm_pass"
        set_env IG_DM_SESSIONID ""
    fi
    return 0
}

do_igdirect() {
    echo; info "=== اینستاگرام دایرکت (دایرکت اینستا -> تلگرام) ==="; echo
    echo "  1) رسمی (وب‌هوک متا)"
    echo "     نیاز: اکانت دولوپر وریفای‌شده‌ی متا + دامنه + App Review"
    echo "     بدون App Review فقط ۲۵ اکانت تستر کار می‌کنن."
    echo
    echo "  2) فقط پشتیبان (instagrapi)"
    echo "     نیاز: هیچی از متا. از روز اول برای همه‌ی کاربرها کار می‌کنه."
    echo "     ریسک: نقض شرایط اینستاگرام، احتمال بسته شدن اکانت."
    echo
    echo "  3) هر دو - رسمی اصلی، پشتیبان وقتی رسمی بیفته بیدار می‌شه"
    echo "  0) انصراف"
    echo
    read -rp "انتخاب: " mode

    local want_official=0 want_standby=0
    case "$mode" in
        1) want_official=1 ;;
        2) want_standby=1 ;;
        3) want_official=1; want_standby=1 ;;
        *) return 0 ;;
    esac

    IGD_DOMAIN=""; IGD_PATH=""; IGD_PORT=""; IGD_VERIFY=""

    if (( want_official )); then
        warn "قبلش این‌ها باید انجام شده باشه:"
        echo "  • اکانت اینستاگرام بات روی Professional باشه"
        echo "  • تو اپ اینستا: Settings > Messages > اجازه دسترسی ابزارهای متصل"
        echo "  • یه اپ روی developers.facebook.com با محصول Instagram"
        echo "  • یه ساب‌دامنه که رکورد A ش به همین سرور اشاره کنه"
        echo
        read -rp "ادامه بدم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 0
        _igdirect_official || return 1
    fi

    if (( want_standby )); then
        _igdirect_standby || want_standby=0
    fi

    # The web source counts as configured once all three browser cookies are
    # present. _igdirect_standby already set IG_DIRECT_SOURCES when it
    # collected them, and this block used to overwrite that with a bare
    # "poll" - so the web reader was set up correctly and then switched off
    # one line later.
    local sources="" have_web=0
    if [[ -n "$(get_env IG_DM_CSRFTOKEN)" && -n "$(get_env IG_DM_DS_USER_ID)" ]]; then
        sources="web"
        have_web=1
    fi
    # The mobile path is only added when the web one is NOT available. On an
    # account whose browser cookie is what we hold, instagrapi is refused
    # every time - and each attempt is more refused traffic against the very
    # account we are trying to keep alive.
    if (( want_standby && !have_web )); then
        sources="${sources:+$sources,}poll"
    fi
    # Realtime is installed out of band by `botctl igmqtt`, which records the
    # choice by putting mqtt in this same variable. Rebuilding the list from
    # scratch here threw that away, so refreshing a cookie silently demoted the
    # account back to polling - the identical overwrite the web source was
    # losing to above, one source further along. It reads the same cookie web
    # does, so it is available on exactly the same condition.
    if (( have_web )) && [[ "$(get_env IG_DIRECT_SOURCES)" == *mqtt* ]]; then
        sources="mqtt${sources:+,$sources}"
    fi
    if (( want_official )); then
        sources="webhook${sources:+,$sources}"
    fi

    if [[ -z "$sources" ]]; then
        err "هیچ مسیری تنظیم نشد"
        return 1
    fi
    set_env IG_DIRECT_SOURCES "$sources"
    ok "منابع: $sources"

    chmod 600 "$PROJECT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env"
    systemctl restart "$SERVICE_NAME"
    sleep 3

    echo; info "--- تست ---"
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        err "بات بالا نیومد - لاگ:"
        journalctl -u "$SERVICE_NAME" --no-pager -n 25
        return 1
    fi
    ok "بات بالاست"

    if (( want_official )); then
        if curl -sf --max-time 5 "http://127.0.0.1:$IGD_PORT/healthz" >/dev/null; then
            ok "لیسنر بالاست"
        else
            err "لیسنر جواب نمی‌ده - لاگ:"
            journalctl -u "$SERVICE_NAME" --no-pager -n 20
        fi
        if curl -sf --max-time 15 "https://$IGD_DOMAIN/healthz" >/dev/null; then
            ok "از بیرون هم با HTTPS در دسترسه"
        else
            warn "از بیرون جواب نداد. سرتیفیکیت شاید هنوز صادر نشده: journalctl -u caddy -n 30"
        fi

        echo
        ok "حالا تو داشبورد متا این‌ها رو بذار:"
        echo -e "   ${B}Callback URL:${N}  https://$IGD_DOMAIN$IGD_PATH"
        echo -e "   ${B}Verify Token:${N}  $IGD_VERIFY"
        echo -e "   ${B}Webhook field:${N} messages"
        echo
        warn "یادت باشه: با Standard Access فقط اکانت‌هایی که تو App Dashboard نقش دارن"
        warn "پیامشون میاد. برای بقیه باید App Review بزنی."
    fi

    if (( want_standby )); then
        echo
        info "مسیر پشتیبان تنظیم شد. اولین لاگین چند ثانیه طول می‌کشه."
        info "اگه اینستاگرام چالش یا کد تایید خواست، تو لاگ می‌بینیش:  botctl logs"
        info "کاربرها باید به @$(get_env IG_DM_USERNAME) دایرکت بدن."
    fi

    echo
    info "وضعیت رو تو بات با /srcstatus ببین."
}

do_proxy() {
    echo; info "=== پروکسی (وقتی IP سرور رد شده) ==="; echo
    warn "نشانه‌ش اینه که سرویس‌های بی‌ربط هم‌زمان ۴۰۳ HTML می‌دن:"
    echo "  • شزم:      403 Forbidden از amp.shazam.com"
    echo "  • اینستاگرام: 403 Forbidden از i.instagram.com"
    echo "  اگه هر دو با هم خرابن، مشکل IP ـه نه کد."
    echo
    info "تست فعلی از این سرور:"
    local sc
    sc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://amp.shazam.com/ 2>/dev/null)
    echo "  amp.shazam.com     -> HTTP ${sc:-timeout}"
    sc=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://i.instagram.com/api/v1/ 2>/dev/null)
    echo "  i.instagram.com    -> HTTP ${sc:-timeout}"
    echo

    info "هم http:// و هم socks5:// کار می‌کنن (کتابخونه‌هاش خودکار نصب می‌شن)."
    echo

    echo
    err "پروکسی رایگان از اینترنت برندار."
    warn "کوکی sessionid اینستاگرامت از داخل پروکسی رد می‌شه. هرکی پروکسی رو"
    warn "داره می‌تونه باهاش مستقیم وارد اکانتت بشه - بدون پسورد، بدون 2FA."
    echo
    echo "  1) Cloudflare WARP روی همین سرور (رایگان، خودکار)"
    echo "  2) یه سرور دیگه‌ی خودت به‌عنوان پروکسی (بهترین گزینه اگه داری)"
    echo "  3) پروکسی‌ای که خودم دارم رو وارد می‌کنم"
    echo "  4) خاموش کردن پروکسی (برگشت به حالت مستقیم)"
    echo "  5) خاموش کردن فقط برای اینستاگرام (شازام روی پروکسی می‌مونه)"
    echo "  6) یوتیوب رو از پروکسی رد کن / بردار (برای «not a bot»)"
    echo "  0) برگرد"
    echo
    read -rp "انتخاب: " how

    local p=""
    case "$how" in
        4)
            set_env SHAZAM_PROXY ""
            set_env IG_DM_PROXY  ""
            systemctl restart "$SERVICE_NAME"
            ok "پروکسی خاموش شد - ترافیک مستقیم می‌ره"
            info "برای قطع کامل WARP هم:  warp-cli --accept-tos disconnect"
            return 0
            ;;
        5)
            # WARP is anycast: its exit address is not promised to stay put,
            # and a sessionid is bound to the address it was issued to. Shazam
            # does not care - it has no session - but Instagram does, so these
            # two stopped being one setting. Going direct trades an address
            # Instagram distrusts for one that at least never moves.
            local ig_ip
            ig_ip=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null)
            set_env IG_DM_PROXY ""
            chmod 600 "$PROJECT_DIR/.env"
            chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env"
            systemctl restart "$SERVICE_NAME"
            ok "اینستاگرام مستقیم شد. شازام روی پروکسی موند."
            echo
            warn "کوکی فعلی از IP دیگه‌ای ساخته شده، پس همین الان باطله."
            warn "کوکی تازه باید از هوای همین سرور گرفته شه: ${ig_ip:-<IP همین سرور>}"
            echo
            info "رو کامپیوتر خودت (نه سرور) یه تونل بزن:"
            echo "    ssh -D 1080 -N root@${ig_ip:-<IP همین سرور>}"
            echo
            info "بعد مرورگر رو روی socks5://127.0.0.1:1080 بذار، اینستاگرام"
            info "لاگین کن، sessionid رو بردار، و اینجا:"
            echo "    botctl igdirect  →  گزینه ۲"
            echo
            info "تست:  botctl igtest2"
            return 0
            ;;
        6)
            # YouTube's bot check is decided by the address, so the cheapest
            # thing to try before handing it an account is a different exit.
            # Kept separate from the Instagram and Shazam settings: all three
            # are refused by different things at different times, and one
            # setting for all of them meant fixing one broke another.
            local cur
            cur=$(get_env YT_PROXY)
            if [[ -n "$cur" ]]; then
                info "الان یوتیوب از این رد می‌شه: $cur"
                read -rp "برش دارم؟ (y/n) " a
                if [[ "$a" == "y" ]]; then
                    set_env YT_PROXY ""
                    systemctl restart "$SERVICE_NAME"
                    ok "یوتیوب برگشت به مستقیم"
                fi
                return 0
            fi

            local yp
            yp=$(get_env SHAZAM_PROXY)
            [[ -z "$yp" ]] && yp="socks5h://127.0.0.1:40000"
            read -rp "آدرس پروکسی برای یوتیوب [$yp]: " a
            [[ -n "$a" ]] && yp="$a"

            info "تست..."
            local code
            code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
                --proxy "$yp" https://www.youtube.com/ 2>/dev/null)
            echo "  youtube.com -> HTTP ${code:-timeout}"
            if [[ -z "$code" || "$code" == "000" ]]; then
                err "پروکسی جواب نداد - ذخیره نکردم"
                return 1
            fi

            set_env YT_PROXY "$yp"
            systemctl restart "$SERVICE_NAME"
            ok "یوتیوب از پروکسی رد می‌شه"
            echo
            info "یه ویدیو تست کن، بعد:  botctl find 'yt-dlp client'"
            warn "اگه بازم «not a bot» گرفتی، این IP هم دیتاسنتریه و"
            warn "تنها راه باقی‌مونده کوکیه:  botctl ytcookies"
            return 0
            ;;
        1) _proxy_warp || return 1; p="socks5h://127.0.0.1:40000" ;;
        2)
            echo
            info "روی اون سرور (نه این یکی) اینو بزن:"
            echo "    apt install -y tinyproxy"
            echo "    # /etc/tinyproxy/tinyproxy.conf :"
            echo "    #   Port 8888"
            echo "    #   Allow $(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || echo '<IP همین سرور>')"
            echo "    systemctl restart tinyproxy"
            warn "حتما فقط IP همین سرور رو Allow کن، وگرنه پروکسی بازِ عمومی می‌شه."
            echo
            read -rp "آدرس پروکسی (http://IP:8888): " p
            ;;
        3) read -rp "آدرس پروکسی: " p ;;
        *) return 0 ;;
    esac

    [[ -z "$p" ]] && { warn "چیزی وارد نشد"; return 0; }

    # Test before saving. A proxy that does not unblock these two hosts is
    # worse than none: it adds a hop, and for instagrapi it also adds someone
    # who can read the session cookie.
    echo; info "تست پروکسی..."
    local ok_sz ok_ig
    ok_sz=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        --proxy "$p" https://amp.shazam.com/ 2>/dev/null)
    ok_ig=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        --proxy "$p" https://i.instagram.com/api/v1/ 2>/dev/null)
    echo "  amp.shazam.com   -> HTTP ${ok_sz:-timeout}"
    echo "  i.instagram.com  -> HTTP ${ok_ig:-timeout}"

    # 000 is curl for "no response at all" - the proxy itself is unreachable.
    # The first version only rejected 403 and empty, so 000 was reported as
    # success and a completely dead proxy got saved.
    if [[ -z "$ok_sz" || "$ok_sz" == "000" ]]; then
        err "پروکسی جواب نداد (HTTP 000) - یعنی خود پروکسی بالا نیست."
        echo "   چک کن:"
        echo "     ss -lntp | grep -E '8118|40000'"
        echo "     systemctl status privoxy --no-pager -l | tail -20"
        echo "     warp-cli --accept-tos status"
        echo "     curl -x http://127.0.0.1:8118 -s -o /dev/null -w '%{http_code}\\n' https://api.ipify.org"
        read -rp "بازم ذخیره کنم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 1
    elif [[ "$ok_sz" == "403" ]]; then
        err "پروکسی وصله ولی شزم به IP اونم ۴۰۳ می‌ده."
        read -rp "بازم ذخیره کنم؟ (y/n) " a
        [[ "$a" != "y" ]] && return 1
    else
        ok "پروکسی جواب می‌ده (HTTP $ok_sz)"
    fi

    # Both take socks now: instagrapi through requests[socks], shazamio
    # through aiohttp-socks. Make sure those are present for a socks url that
    # arrived by some other route than the WARP path above.
    if [[ "$p" == socks* ]]; then
        sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --progress-bar off \
            "aiohttp-socks" "requests[socks]" "httpx[socks]" 2>/dev/null || \
            warn "نصب کتابخونه‌های socks شکست خورد - ممکنه پروکسی نادیده گرفته بشه"
    fi
    set_env SHAZAM_PROXY "$p"
    set_env IG_DM_PROXY  "$p"
    ok "هر دو ست شدن"

    chmod 600 "$PROJECT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env"
    systemctl restart "$SERVICE_NAME"
    sleep 3
    ok "ریستارت شد."

    # A sessionid is tied to the address it was issued to. Changing the proxy
    # changes the exit country, and Instagram invalidates the cookie for it -
    # a login that worked directly stops working the moment a proxy is added,
    # and it looks like the proxy is broken when it is not.
    if [[ -n "$(get_env IG_DM_SESSIONID)" ]]; then
        echo
        warn "کوکی sessionid فعلی با IP قبلی ساخته شده و حالا کشور خروجی عوض شده."
        warn "اینستاگرام معمولا همون‌جا باطلش می‌کنه. یه sessionid تازه بگیر:"
        echo "    botctl igdirect  →  گزینه ۲  →  sessionid جدید"
    fi
    echo
    ok "تست:  botctl shazamtest   و   botctl igtest2"
}

_proxy_warp() {
    # WARP is Cloudflare's own network. It is free, needs no account, and the
    # traffic leaves from a Cloudflare address instead of this datacenter's -
    # which is the entire point here. It only speaks SOCKS5, so privoxy sits
    # in front to give shazamio the http proxy aiohttp requires.
    echo; info "نصب Cloudflare WARP..."

    if ! command -v warp-cli &>/dev/null; then
        curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
            | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
            > /etc/apt/sources.list.d/cloudflare-client.list
        apt-get update -qq
        apt-get install -y -qq cloudflare-warp || { err "نصب WARP شکست خورد"; return 1; }
    fi

    # The registration subcommand was renamed between versions.
    warp-cli --accept-tos registration new 2>/dev/null \
        || warp-cli --accept-tos register 2>/dev/null || true
    warp-cli --accept-tos mode proxy       || { err "warp-cli mode proxy نشد"; return 1; }
    warp-cli --accept-tos proxy port 40000 2>/dev/null || true
    warp-cli --accept-tos connect          || { err "warp-cli connect نشد"; return 1; }
    sleep 3

    if ! curl -s --max-time 15 --proxy socks5h://127.0.0.1:40000 https://api.ipify.org >/dev/null; then
        err "WARP بالا نیومد. وضعیت:"; warp-cli --accept-tos status
        return 1
    fi
    ok "WARP وصل شد - IP خروجی: $(curl -s --max-time 15 --proxy socks5h://127.0.0.1:40000 https://api.ipify.org)"

    # No privoxy bridge. Both libraries can speak SOCKS directly once their
    # optional dependency is present, and the bridge was one more service to
    # install, start and get wrong - which it did immediately: the setup
    # appended a listen-address the packaged config already had, privoxy
    # refused to start binding the same port twice, and every request through
    # it failed with connection refused.
    info "نصب کتابخونه‌های SOCKS (به‌جای privoxy)..."
    # One per http library in use: aiohttp for shazamio, requests for
    # instagrapi, httpx for the web inbox reader. Missing any one of them
    # makes that path ignore the proxy or refuse the url outright.
    if ! sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install --progress-bar off \
            "aiohttp-socks" "requests[socks]" "httpx[socks]"; then
        err "نصب کتابخونه‌های socks شکست خورد"
        return 1
    fi
    ok "نصب شد - شزم، اینستاگرام و API وب هر سه مستقیم socks5 می‌زنن"

    # Prove the venv itself can reach out through the proxy, not just curl.
    if run_py - <<'PY'
import sys
try:
    import requests
    r = requests.get("https://api.ipify.org",
                     proxies={"https": "socks5h://127.0.0.1:40000"}, timeout=20)
    print(f"    python از طریق socks -> {r.text.strip()}")
    sys.exit(0 if r.ok else 1)
except Exception as e:
    print(f"    python از طریق socks شکست خورد: {type(e).__name__}: {e}")
    sys.exit(1)
PY
    then
        ok "venv هم از پروکسی رد می‌شه"
        return 0
    fi

    err "venv نتونست از socks رد بشه. وضعیت:"
    ss -lntp 2>/dev/null | grep -E ':40000' || echo "    پورت 40000 باز نیست"
    warp-cli --accept-tos status 2>&1 | head -5
    return 1
}

do_igreset() {
    echo; info "=== ریست سشن اینستاگرام دایرکت ==="; echo
    warn "این کار سشن ذخیره‌شده رو پاک می‌کنه و بات دوباره لاگین می‌کنه."
    warn "قبلش حتما این‌ها رو انجام بده وگرنه دوباره بلاک می‌شه:"
    echo "  1. با همون اکانت تو اپ موبایل اینستاگرام لاگین کن"
    echo "  2. اگه پیام امنیتی/تایید داد، تاییدش کن"
    echo "  3. چند دقیقه عادی باهاش کار کن (اسکرول، لایک)"
    echo
    read -rp "انجام دادی؟ (y/n) " a
    [[ "$a" != "y" ]] && { warn "لغو شد"; return 0; }

    rm -f "$PROJECT_DIR/downloads/ig_private_session.json"
    ok "سشن پاک شد"
    # NOT ig_device.json. Deleting that makes instagrapi invent a new phone,
    # Instagram sees an unknown device on the account, and the direct
    # endpoints start answering 403/1404006 while the login still succeeds.
    # That is what this command caused the first time it was run.
    if [[ -f "$PROJECT_DIR/downloads/ig_device.json" ]]; then
        ok "device fingerprint حفظ شد (پاک کردنش باعث ۴۰۳ می‌شه)"
    fi

    local cur
    cur=$(get_env IG_DM_POLL_SECONDS)
    echo
    info "فاصله پولینگ فعلی: ${cur:-8}s"
    warn "پولینگ سریع همون چیزیه که اکانت رو بلاک کرد."
    read -rp "بذارمش روی ۱۵ ثانیه (امن‌تر)؟ (y/n) " a
    if [[ "$a" == "y" ]]; then
        set_env IG_DM_POLL_SECONDS 15
        set_env IG_DM_FAST_SECONDS 5
        ok "روی ۱۵/۵ ثانیه تنظیم شد"
    fi

    chown -R "$BOT_USER:$BOT_USER" "$PROJECT_DIR/downloads"
    systemctl restart "$SERVICE_NAME"
    sleep 4
    info "لاگ:"
    journalctl -u "$SERVICE_NAME" --no-pager -n 15 | grep -iE "ig poll|blocked" || echo "  (چیزی نیست - خوبه)"
    echo
    warn "اگه دوباره ۴۰۳ داد، اکانت هنوز فلگه. یکی دو روز دست بهش نزن."
}

do_igmqtt() {
    echo; info "=== دایرکت بدون پولینگ (MQTT) ==="; echo
    warn "چرا این فرق داره:"
    echo "  اپ واقعی اینستاگرام هیچ‌وقت اینباکس رو poll نمی‌کنه - یه اتصال"
    echo "  دائمی MQTT باز می‌کنه و منتظر می‌مونه. هر فاصله‌ی پولینگی که"
    echo "  انتخاب کنیم، شکل ترافیک همونیه که اپ هیچ‌وقت تولید نمی‌کنه."
    echo
    echo "  با این: صفر درخواست در ساعت، و تحویل به محض رسیدن پیام."
    echo
    warn "پشتیبانی realtime تو aiograpi آزمایشیه و اینستاگرام می‌تونه"
    warn "این کانال خصوصی رو بدون اطلاع عوض کنه. اگه وصل نشد، بات"
    warn "خودکار برمی‌گرده به پولینگ فعلی - چیزی از دست نمی‌ره."
    echo
    read -rp "نصب کنم؟ (y/n) " a
    [[ "$a" != "y" ]] && return 0

    # Separate install for the same reason instagrapi is: a resolver problem
    # here must not be able to break a working venv on the next update.
    if ! sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install --progress-bar off aiograpi; then
        err "نصب aiograpi شکست خورد - بات با پولینگ ادامه می‌ده"
        return 1
    fi

    local sources
    sources=$(get_env IG_DIRECT_SOURCES)
    [[ "$sources" != *mqtt* ]] && set_env IG_DIRECT_SOURCES "mqtt,${sources:-web}"

    systemctl restart "$SERVICE_NAME"
    sleep 5
    ok "نصب شد. تو لاگ دنبال این بگرد:"
    echo "    ig mqtt: realtime connected - polling is no longer needed"
    echo
    info "اگه وصل نشد، این خط رو می‌بینی و بات با پولینگ ادامه می‌ده:"
    echo "    ig direct: realtime down (...) - falling back to polling"
    echo
    info "بعدش:  botctl logs   و   /srcstatus"
}

do_igwatch() {
    echo; info "=== نگاه زنده به زنجیره‌ی دایرکت ==="
    # "Nothing arrives" has six possible causes that look identical from the
    # outside. This shows every stage, so whichever one drops the message is
    # the one you see.
    run_py         "$PROJECT_DIR/deploy/igwatch.py" "${1:-120}"
}

do_igtest() {
    echo; info "=== تست زنده‌ی اینستاگرام دایرکت ==="
    # The bot's log only shows the end of the story. This runs the same
    # sequence step by step so a refused address, a stale session, a rejected
    # device and a real account problem stop looking identical.
    run_py "$PROJECT_DIR/deploy/igtest.py"
}

do_backup() {
    # Default to a dated name in /root: the file is a credential, and /root
    # is the one place on a fresh box that is not world-readable.
    local out="${1:-/root/moeinimydl-backup-$(date +%Y%m%d-%H%M).tar.gz}"
    run_py_root "$PROJECT_DIR/deploy/backup.py" create "$out" || return 1
    chmod 600 "$out" 2>/dev/null || true
}

do_restore() {
    local src="$1"
    if [[ -z "$src" ]]; then
        err "کدوم فایل؟  botctl restore /root/moeinimydl-backup-....tar.gz"
        return 1
    fi
    if [[ ! -f "$src" ]]; then
        err "فایل پیدا نشد: $src"
        return 1
    fi

    echo; info "=== برگرداندن بکاپ ==="
    warn "این کار .env و کش و سشن اینستاگرام فعلی رو با نسخه‌ی داخل بکاپ عوض می‌کنه."
    read -r -p "مطمئنی؟ [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { info "لغو شد."; return 1; }

    # Stopped for the whole restore. stats.db and the Instagram cookie jar
    # are both written by the running bot, and putting a file underneath a
    # process that has it open is how a restore corrupts the thing it was
    # supposed to rescue.
    local was_running=0
    systemctl is-active --quiet "$SERVICE_NAME" && was_running=1
    if (( was_running )); then
        info "موقتا متوقف می‌کنم…"
        systemctl stop "$SERVICE_NAME"
    fi

    # As root: the archive is root-owned and the restored files have to land
    # on top of ones the bot user owns. backup.py gives each restored file
    # the owner of the directory it lands in, so the bot can still write its
    # own cache and rotate its own Instagram cookie afterwards.
    local rc=0
    run_py_root "$PROJECT_DIR/deploy/backup.py" restore "$src" || rc=$?

    chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env" 2>/dev/null || true
    chmod 600 "$PROJECT_DIR/.env" 2>/dev/null || true

    if (( was_running )); then
        info "دوباره روشن می‌کنم…"
        systemctl start "$SERVICE_NAME"
    fi
    return $rc
}

do_igprobe() {
    echo; info "=== تست اندپوینت‌های پروفایل / استوری / هایلایت ==="
    # do_igtest covers the DIRECT side - can this session read the inbox.
    # This covers the other three buttons, which use different endpoints and
    # fail for different reasons. "None of the three options work" and "the
    # session is fine but Instagram retired one endpoint" look identical from
    # the chat, and only asking each endpoint separately tells them apart.
    run_py "$PROJECT_DIR/deploy/igprobe.py" "${1:-instagram}"
}

do_shazamtest() {
    echo; info "=== تست زنده‌ی شزم ==="
    # "FailedDecodeJson" only says the body was not json. Whether that body
    # was a block page, a 403, a captcha or an empty response wants different
    # answers, and none of them are visible from the bot's error message.
    # Only pass a path when there is one. "${1:-}" hands the script an empty
    # argument, which Path("") reads as "." - so the menu entry reported
    # "[X] . not found" instead of picking a file itself.
    if [[ -n "${1:-}" ]]; then
        run_py \
            "$PROJECT_DIR/deploy/shazamtest.py" "$1"
    else
        run_py \
            "$PROJECT_DIR/deploy/shazamtest.py"
    fi
}

do_deps() {
    echo; info "=== وضعیت پکیج‌ها و دیسک ==="; echo

    # First, because it is the failure that does not announce itself: with no
    # room to write a clip, ffmpeg fails, every recognition window comes back
    # empty, and the bot tells the user it found no music.
    info "دیسک:"
    df -h "$PROJECT_DIR" | tail -1
    local free_mb
    free_mb=$(df -m --output=avail "$PROJECT_DIR" 2>/dev/null | tail -1 | tr -d ' ')
    if [[ -n "$free_mb" ]] && (( free_mb < 500 )); then
        err "فقط ${free_mb}MB آزاده - تشخیص آهنگ با این فضا کار نمی‌کنه"
        echo "   بزرگ‌ترین‌ها تو downloads:"
        du -sh "$PROJECT_DIR/downloads"/* 2>/dev/null | sort -rh | head -8
        warn "پاک کردن:  botctl clearcache"
    else
        ok "فضای کافی هست"
    fi
    echo

    local py="$PROJECT_DIR/.venv/bin/python"
    local pip="$PROJECT_DIR/.venv/bin/pip"

    info "نسخه‌ها:"
    "$pip" list 2>/dev/null | grep -iE '^(shazamio|shazamio-core|pydantic|pydantic-core|aiohttp|instagrapi|faster-whisper|ctranslate2|yt-dlp|python-telegram-bot|httpx)\b' \
        || warn "pip list جواب نداد"

    # Not a pip package, so pip list cannot show it - and yt-dlp being current
    # means nothing for YouTube if this is missing. An up-to-date yt-dlp and a
    # deno that would not run produced "Requested format is not available" on
    # every client, with nothing in the versions above to suggest why.
    local dv
    dv=$(deno_version)
    [[ -n "$dv" ]] && ok "deno  $dv" \
                   || err "deno نیست یا اجرا نمی‌شه - یوتیوب بدون این جواب نمی‌ده (botctl ytdlp)"
    echo

    # The reason this exists: instagrapi and faster-whisper were installed
    # into the same venv as shazamio, and all four have opinions about
    # pydantic. A resolver conflict there breaks music recognition silently -
    # nothing logs it, the feature just stops matching.
    info "تضاد وابستگی‌ها (pip check):"
    if "$pip" check 2>&1 | grep -v '^$'; then
        warn "بالا رو بخون - تضاد یعنی یکی از فیچرها بی‌صدا خرابه"
    else
        ok "تضادی نیست"
    fi
    echo

    info "ایمپورت واقعی هر کدوم:"
    "$py" - <<'PY'
mods = [
    ("shazamio", "تشخیص آهنگ"),
    ("pydantic", "-"),
    ("aiohttp", "-"),
    ("instagrapi", "دایرکت اینستاگرام"),
    ("faster_whisper", "زیرنویس"),
    ("yt_dlp", "دانلود"),
]
for name, what in mods:
    try:
        m = __import__(name)
        v = getattr(m, "__version__", "?")
        print(f"  OK   {name:16} {v:12} {what}")
    except Exception as e:
        print(f"  FAIL {name:16} {'':12} {what}  <- {type(e).__name__}: {e}")
PY
    echo
    info "تست زنده‌ی شزم:"
    "$py" - <<'PY'
import asyncio, socket, sys
sys.path.insert(0, ".")
try:
    socket.create_connection(("amp.shazam.com", 443), timeout=6).close()
    print("  OK   amp.shazam.com قابل دسترسه")
except Exception as e:
    print(f"  FAIL amp.shazam.com در دسترس نیست: {e}")
try:
    from shazamio import Shazam
    s = Shazam()
    print("  OK   کلاینت شزم ساخته شد:", ", ".join(
        m for m in ("recognize", "recognize_song") if hasattr(s, m)) or "هیچ متد شناخته‌شده‌ای نداره!")
except Exception as e:
    print(f"  FAIL ساخت کلاینت شزم: {type(e).__name__}: {e}")
PY
}

do_whisper() {
    echo; info "=== زیرنویس ویدیو ==="; echo
    echo "  1) API (پیشنهادی) — هیچ باری روی این سرور نمیاد"
    echo "     whisper-large-v3، بهترین کیفیت فارسی، چند ثانیه."
    echo "     Groq لایه رایگان داره: روزی ۸ ساعت صدا."
    echo
    echo "  2) محلی (faster-whisper) — بدون اکانت، ولی CPU این سرور رو می‌خوره"
    echo "  3) خاموش کردن زیرنویس"
    echo "  0) انصراف"
    echo
    read -rp "انتخاب: " mode

    case "$mode" in
        1)
            echo
            info "کلید رایگان: https://console.groq.com/keys"
            warn "کلید رو من نمی‌بینم؛ مستقیم تو .env همین سرور ذخیره می‌شه."
            local key
            read -rsp "API key: " key; echo
            [[ -z "$key" ]] && { err "کلید خالی بود"; return 1; }

            set_env WHISPER_API_URL   "https://api.groq.com/openai/v1"
            set_env WHISPER_API_MODEL "whisper-large-v3"
            set_env WHISPER_API_KEY   "$key"
            chmod 600 "$PROJECT_DIR/.env"
            chown "$BOT_USER:$BOT_USER" "$PROJECT_DIR/.env"
            systemctl restart "$SERVICE_NAME"
            sleep 2
            ok "فعال شد. دکمه «زیرنویس ویدیو» حالا از API استفاده می‌کنه."
            info "مدل محلی اگه نصب باشه به‌عنوان پشتیبان می‌مونه."
            ;;
        2)
            local total_mb
            total_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
            echo; info "رم این سرور: ${total_mb}MB · هسته: $(nproc 2>/dev/null || echo '?')"
            echo
            warn "برای فارسی سایز مدل همه‌چیزه، و هزینه‌ش زمان CPU ـه:"
            echo "  small           فارسیش تقریبا ساختگیه.       ~۵۰۰MB"
            echo "  medium          فارسی خونا می‌شه.             ~۱.۵GB"
            echo "  large-v3-turbo  فارسی درست، ولی خیلی کند.    ~۲GB"
            echo
            warn "تجربه‌ی واقعی روی همین سرور: large-v3-turbo برای یه کلیپ ۵ دقیقه طول کشید."
            (( total_mb < 2500 )) && warn "با ${total_mb}MB رم، large ممکنه OOM بده."

            read -rp "کدوم مدل؟ [medium]: " model
            model="${model:-medium}"
            case "$model" in
                tiny|base|small|medium|large-v3-turbo|large-v3|turbo) ;;
                *) err "مدل نامعتبر"; return 1 ;;
            esac

            # >=1.1 for the large-v3-turbo alias and detect_language().
            # Deliberately not in requirements.txt: it pulls ctranslate2 and a
            # model download, and that must not break a working venv on update.
            if ! sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install --progress-bar off \
                    "faster-whisper>=1.1.0"; then
                err "نصب faster-whisper شکست خورد"
                return 1
            fi

            set_env WHISPER_MODEL "$model"
            # Half the cores. Left unset, ctranslate2 takes every one of them
            # and the bot cannot download anything while it transcribes.
            local half
            half=$(( $(nproc 2>/dev/null || echo 2) / 2 )); (( half < 1 )) && half=1
            set_env WHISPER_CPU_THREADS "$half"
            info "روی $half هسته اجرا می‌شه تا بقیه‌ی بات بیکار نمونه."

            chown -R "$BOT_USER:$BOT_USER" "$PROJECT_DIR/downloads"
            systemctl restart "$SERVICE_NAME"
            ok "نصب شد."
            warn "اولین استفاده مدل رو دانلود می‌کنه و کنده."
            ;;
        3)
            set_env WHISPER_API_KEY ""
            systemctl restart "$SERVICE_NAME"
            ok "API خاموش شد."
            info "برای حذف کامل مدل محلی:  $PROJECT_DIR/.venv/bin/pip uninstall -y faster-whisper"
            ;;
        *) return 0 ;;
    esac
}

_botapi_grant_read() {
    # The server writes new media with its own umask, so a one-off chmod only
    # fixes files that already exist - the next video is unreadable again and
    # python-telegram-bot falls back to an HTTP fetch the local server answers
    # with 404 ("Not Found"). A DEFAULT ACL is inherited by files created
    # later, which is what actually makes this stick.
    local data_dir="/var/lib/telegram-bot-api"
    [[ -d "$data_dir" ]] || return 0

    command -v setfacl &>/dev/null || {
        info "نصب پکیج acl..."
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq acl &>/dev/null
    }

    if command -v setfacl &>/dev/null; then
        setfacl -R  -m "u:$BOT_USER:rX" "$data_dir" 2>/dev/null
        setfacl -R -d -m "u:$BOT_USER:rX" "$data_dir" 2>/dev/null
        ok "دسترسی خواندن برای $BOT_USER تنظیم شد (شامل فایل‌های آینده)"
    else
        warn "setfacl نصب نشد - فقط فایل‌های فعلی خوندنی می‌شن"
    fi
    chmod -R a+rX "$data_dir" 2>/dev/null
}


_botapi_run() {
    # One place that knows how to start the container correctly.
    # It must run as ROOT: the image's entrypoint chowns the data directory to
    # its own internal user before dropping privileges. Pinning --user made
    # that chown fail with "Operation not permitted" and the container died in
    # a restart loop.
    local api_id="$1" api_hash="$2" data_dir="/var/lib/telegram-bot-api"

    mkdir -p "$data_dir"
    docker rm -f telegram-bot-api &>/dev/null

    # TELEGRAM_LOCAL, not "--local": this image's entrypoint builds the server
    # command line from environment variables and discards the container's
    # own arguments entirely. Passing --local as an argument looked right and
    # did nothing, so the server kept answering getFile with relative paths
    # and every media download 404'd.
    docker run -d --name telegram-bot-api --restart always \
        -p 127.0.0.1:8081:8081 \
        -e TELEGRAM_API_ID="$api_id" \
        -e TELEGRAM_API_HASH="$api_hash" \
        -e TELEGRAM_LOCAL=1 \
        -e TELEGRAM_DIR="$data_dir" \
        -v "$data_dir:$data_dir" \
        aiogram/telegram-bot-api:latest \
        || { err "docker بالا نیومد"; return 1; }

    info "صبر برای بالا اومدن سرور..."
    local i
    for i in $(seq 1 12); do
        sleep 2
        if curl -s -m 3 -o /dev/null "http://127.0.0.1:8081/"; then
            ok "سرور بالا اومد"
            # Confirm --local really reached the server. Without it getFile
            # answers with relative paths, files are never written where the
            # bot looks, and every download fails with "Not Found".
            if docker logs telegram-bot-api 2>&1 | grep -q -- '--local'; then
                ok "حالت local فعاله"
            else
                err "حالت local فعال نشد! دستور اجراشده:"
                docker logs telegram-bot-api 2>&1 | grep 'telegram-bot-api --' \
                    | tail -1 | sed 's/^/    /'
                warn "بدون این، دانلود فایل‌های بزرگ کار نمی‌کنه."
            fi
            _botapi_grant_read
            return 0
        fi
    done

    err "سرور جواب نمی‌ده. لاگ کانتینر:"
    docker logs --tail 25 telegram-bot-api 2>&1 | sed 's/^/    /'
    return 1
}


do_botapi_repair() {
    echo; info "=== تعمیر کانتینر Local Bot API ==="
    if ! docker inspect telegram-bot-api &>/dev/null; then
        err "کانتینری وجود نداره - گزینه ۹ رو بزن"
        return 1
    fi

    # Reuse the credentials already in the container so they need not be
    # typed again just to rebuild it.
    local api_id api_hash env_dump
    env_dump=$(docker inspect telegram-bot-api --format '{{range .Config.Env}}{{println .}}{{end}}')
    api_id=$(echo "$env_dump"   | grep '^TELEGRAM_API_ID='   | cut -d= -f2-)
    api_hash=$(echo "$env_dump" | grep '^TELEGRAM_API_HASH=' | cut -d= -f2-)

    if [[ -z "$api_id" || -z "$api_hash" ]]; then
        err "API ID/HASH تو کانتینر نبود - گزینه ۹ رو بزن و دستی واردشون کن"
        return 1
    fi
    ok "کلیدها از کانتینر قبلی خونده شد (API ID: $api_id)"

    _botapi_run "$api_id" "$api_hash" || return 1

    systemctl restart "$SERVICE_NAME"
    sleep 3
    systemctl is-active --quiet "$SERVICE_NAME" \
        && ok "بات بالا اومد" \
        || { err "بات بالا نیومد:"; journalctl -u "$SERVICE_NAME" --no-pager -n 15; }
}


do_fixperms() {
    echo; info "=== اصلاح دسترسی فایل‌های Local Bot API ==="
    local data_dir="/var/lib/telegram-bot-api"
    if [[ ! -d "$data_dir" ]]; then
        err "$data_dir وجود نداره - اول گزینه ۹ رو بزن"
        return 1
    fi

    # Ownership must stay with the server's own uid: taking it away stops the
    # container writing and it exits, which takes the bot down with it. The
    # bot only needs read access, so widen the mode instead.
    # Do NOT chown here: the image's entrypoint owns that decision and runs
    # as root to make it. Taking ownership away is what killed the container.
    _botapi_grant_read

    if command -v docker &>/dev/null; then
        if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^telegram-bot-api$'; then
            warn "کانتینر بالا نیست - استارتش می‌کنم..."
            docker start telegram-bot-api 2>/dev/null \
                && ok "کانتینر استارت شد" \
                || err "استارت نشد - گزینه ۹ رو دوباره اجرا کن"
            sleep 3
        fi
    fi

    systemctl restart "$SERVICE_NAME"
    sleep 3
    systemctl is-active --quiet "$SERVICE_NAME" \
        && ok "بات بالا اومد" \
        || err "بات بالا نیومد - گزینه ۶ (خطاهای اخیر) رو ببین"
}


do_diag() {
    echo; info "=== تشخیص مشکل Local Bot API ==="; echo

    info ".env:"
    grep -E '^(BOT_API_BASE_URL|MAX_UPLOAD_MB|ADMIN_IDS|AUDIO_FORMAT)=' \
        "$PROJECT_DIR/.env" 2>/dev/null | sed 's/^/    /' || echo "    (چیزی ست نشده)"
    echo

    if ! command -v docker &>/dev/null; then
        err "docker نصب نیست - Local Bot API فعال نیست"
        return
    fi

    info "کانتینر:"
    docker ps --filter name=telegram-bot-api \
        --format '    {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null
    if ! docker ps --format '{{.Names}}' | grep -q '^telegram-bot-api$'; then
        err "کانتینر telegram-bot-api بالا نیست"
        docker ps -a --filter name=telegram-bot-api --format '    (متوقف) {{.Status}}'
        return
    fi

    echo
    info "نوع mount (باید bind باشه، نه volume):"
    docker inspect telegram-bot-api \
        --format '    {{range .Mounts}}{{.Type}}  {{.Source}} -> {{.Destination}}{{println}}{{end}}' 2>/dev/null
    if docker inspect telegram-bot-api --format '{{range .Mounts}}{{.Type}}{{end}}' 2>/dev/null \
        | grep -q volume; then
        err "mount از نوع volume ـه - بات نمی‌تونه فایل‌ها رو ببینه!"
        warn "گزینه ۹ رو دوباره اجرا کن تا با bind mount ساخته بشه."
    else
        ok "mount درسته"
    fi

    echo
    info "دسترسی کاربر $BOT_USER به فایل‌های واقعی:"
    if [[ -d /var/lib/telegram-bot-api ]]; then
        ls -ld /var/lib/telegram-bot-api | sed 's/^/    /'

        # Reading the top directory proves nothing: the media sits two levels
        # down and the server creates those with restrictive modes. python-
        # telegram-bot silently falls back to an HTTP fetch when it cannot
        # stat the path, and the local server answers that with 404 -> the
        # user sees "Not Found". So test the real file.
        local sample
        sample=$(find /var/lib/telegram-bot-api -type f -path '*/videos/*' 2>/dev/null | head -1)
        [[ -z "$sample" ]] && sample=$(find /var/lib/telegram-bot-api -type f \
            \( -path '*/photos/*' -o -path '*/documents/*' -o -path '*/music/*' \) 2>/dev/null | head -1)

        if [[ -z "$sample" ]]; then
            warn "هنوز فایل رسانه‌ای دانلود نشده - یه ویدیو به بات بفرست و دوباره بزن"
        else
            ls -l "$sample" | sed 's/^/    /'
            if sudo -u "$BOT_USER" test -r "$sample"; then
                ok "بات می‌تونه فایل رو بخونه"
            else
                err "بات نمی‌تونه این فایل رو بخونه — دلیل «Not Found» همینه"
                warn "گزینه ۹ رو دوباره اجرا کن (کانتینر با کاربر $BOT_USER ساخته می‌شه)"
                warn "یا سریع: bash deploy/manage.sh fixperms"
            fi
        fi
    else
        err "/var/lib/telegram-bot-api روی هاست وجود نداره -> mount اشتباهه"
    fi

    echo
    info "پاسخ سرور محلی:"
    curl -s -m 5 -o /dev/null -w '    HTTP %{http_code}\n' http://127.0.0.1:8081/ || err "جواب نداد"

    echo
    info "حالت local سرور:"
    if docker logs telegram-bot-api 2>&1 | grep -q -- '--local'; then
        ok "فعاله"
    else
        err "فعال نیست — علت اصلی «Not Found» همینه"
        warn "بزن: botctl botapi-repair"
    fi

    echo
    info "آخرین لاگ کانتینر:"
    docker logs --tail 25 telegram-bot-api 2>&1 | sed 's/^/    /' || true
}


do_clearcache() {
    echo; info "=== پاک کردن کش بات ==="
    warn "این‌ها پاک می‌شن:"
    warn "  - شناسه فایل‌های تلگرام (file_ids.json)"
    warn "  - موزیک‌های دانلودشده، کاورها و کش yt-dlp"
    warn ".env و کد دست نمی‌خوره."
    echo
    read -rp "ادامه؟ (y/n) " a
    [[ "$a" != "y" ]] && { warn "لغو شد"; return; }

    systemctl stop "$SERVICE_NAME" 2>/dev/null
    rm -f  "$PROJECT_DIR/downloads/file_ids.json"
    rm -rf "$PROJECT_DIR/downloads/spotify" \
           "$PROJECT_DIR/downloads/thumbs" \
           "$PROJECT_DIR/downloads/recognize" \
           "$PROJECT_DIR/downloads/.ytdlp-cache"
    mkdir -p "$PROJECT_DIR/downloads"
    chown -R "$BOT_USER:$BOT_USER" "$PROJECT_DIR/downloads"
    systemctl start "$SERVICE_NAME"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME" \
        && ok "کش پاک شد و بات دوباره بالا اومد" \
        || err "بات بالا نیومد - گزینه ۶ رو بزن"
}


do_audioformat() {
    echo; info "=== فرمت فایل صوتی ==="
    echo "  1) m4a  (پیشنهادی - سریع‌ترین، بدون تبدیل مجدد)"
    echo "  2) mp3  (سازگاری حداکثری)"
    echo "  3) flac (فایل بزرگ‌تر - منابع یوتیوب/ساندکلاد lossy هستن،"
    echo "          پس کیفیت واقعی بهتر نمی‌شه)"
    echo
    read -rp "انتخاب: " a
    case "$a" in
        1) fmt=m4a ;;
        2) fmt=mp3 ;;
        3) fmt=flac ;;
        *) warn "لغو شد"; return ;;
    esac
    if grep -q '^AUDIO_FORMAT=' "$PROJECT_DIR/.env"; then
        sed -i "s|^AUDIO_FORMAT=.*|AUDIO_FORMAT=$fmt|" "$PROJECT_DIR/.env"
    else
        echo "AUDIO_FORMAT=$fmt" >> "$PROJECT_DIR/.env"
    fi
    systemctl restart "$SERVICE_NAME"
    ok "فرمت روی $fmt تنظیم شد"
}


install_shortcut() {
    ln -sf "$PROJECT_DIR/deploy/manage.sh" /usr/local/bin/botctl
    ok "دستور میانبر ساخته شد: botctl"
}


# ---------------------------------------------------------
# menu
# ---------------------------------------------------------

menu() {
    clear
    local state
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        state="${G}● روشن${N}"
    elif [[ -f "/etc/systemd/system/$SERVICE_NAME.service" ]]; then
        state="${R}● خاموش${N}"
    else
        state="${Y}● نصب نشده${N}"
    fi

    echo -e "${B}=====================================${N}"
    echo -e "${B}   مدیریت ربات دانلودر تلگرام${N}"
    echo -e "${B}=====================================${N}"
    echo -e "   وضعیت: $state"
    is_git_repo && echo -e "   نسخه: $(git -C "$PROJECT_DIR" log -1 --format='%h %s' 2>/dev/null | cut -c1-45)"
    echo -e "${B}=====================================${N}"
    echo
    echo "  1) نصب / اتصال به گیت‌هاب"
    echo "  2) آپدیت از گیت‌هاب  <-- همیشه اینو بزن"
    echo "  3) ریستارت بات"
    echo "  4) وضعیت کامل"
    echo "  5) لاگ زنده"
    echo "  6) فقط خطاهای اخیر"
    echo "  7) ویرایش .env"
    echo "  8) آپدیت yt-dlp (یوتیوب خراب شد؟)"
    echo "  9) فعال کردن آپلود ۲ گیگی"
    echo " 10) ست کردن کوکی اینستاگرام"
    echo " 11) توقف بات"
    echo " 12) پاک کردن کش (کاور/فایل‌های قدیمی)"
    echo " 13) فرمت فایل صوتی (m4a / mp3 / flac)"
    echo " 14) تشخیص مشکل Local Bot API"
    echo " 15) اصلاح دسترسی فایل‌های Bot API"
    echo " 16) تعمیر کانتینر Bot API (بدون وارد کردن کلید)"
    echo " 17) لاگ تشخیص آهنگ"
    echo " 18) کلید اسپاتیفای (پلی‌لیست بزرگ)"
    echo " 19) کانال اجباری (قفل عضویت)"
    echo " 20) وضعیت اینستاگرام"
    echo " 21) موتورهای تشخیص آهنگ"
    echo " 22) اینستاگرام دایرکت (دایرکت اینستا -> تلگرام)"
    echo " 23) زیرنویس ویدیو (faster-whisper)"
    echo " 24) وضعیت پکیج‌ها و تضادها"
    echo " 25) تست زنده‌ی شزم"
    echo " 26) ریست سشن اینستاگرام (بعد از بلاک)"
    echo " 27) پروکسی (شزم / اینستاگرام)"
    echo " 28) تست زنده‌ی اینستاگرام دایرکت"
    echo " 29) نگاه زنده به دایرکت (چرا پیام نمیاد)"
    echo " 30) دایرکت بدون پولینگ (MQTT)"
    echo " 31) نصب مجدد از صفر (پاک کردن همه چی)"
    echo "  0) خروج"
    echo
}

# Non-interactive shortcuts:  botctl install | update | restart | status | logs
case "${1:-}" in
    install) do_install; exit $? ;;
    reset)   do_reset;   exit $? ;;
    clearcache) do_clearcache; exit $? ;;
    diag)    do_diag;    exit 0 ;;
    fixperms) do_fixperms; exit $? ;;
    botapi-repair) do_botapi_repair; exit $? ;;
    reclog)  do_reclog;  exit 0 ;;
    spotify) do_spotify; exit $? ;;
    channel) do_channel; exit $? ;;
    igcheck) do_igcheck; exit 0 ;;
    igdirect) do_igdirect; exit $? ;;
    whisper) do_whisper; exit $? ;;
    deps)    do_deps;    exit 0 ;;
    shazamtest) do_shazamtest "${2:-}"; exit $? ;;
    igtest2) do_igtest; exit $? ;;
    igprobe) do_igprobe "${2:-}"; exit $? ;;
    backup) do_backup "${2:-}"; exit $? ;;
    restore) do_restore "${2:-}"; exit $? ;;
    igwatch) do_igwatch "${2:-}"; exit $? ;;
    igmqtt)  do_igmqtt;  exit $? ;;
    igreset) do_igreset; exit $? ;;
    proxy)   do_proxy;   exit $? ;;
    engines) do_engines; exit $? ;;
    ytdlp)   do_ytdlp;   exit $? ;;
    ytcookies) do_ytcookies; exit $? ;;
    iglogin) do_iglogin "${2:-}"; exit $? ;;
    admin)   do_admin "${2:-}"; exit $? ;;
    igweblogin) do_igweblogin; exit $? ;;
    update)  do_update;  exit $? ;;
    restart) systemctl restart "$SERVICE_NAME"; exit $? ;;
    status)  do_status;  exit 0 ;;
    logs)    do_logs "${2:-}"; exit 0 ;;
    find)    do_find "${2:-}" "${3:-}"; exit $? ;;
    "")      ;;
    *)       err "دستور نامعتبر: $1"; echo "استفاده: botctl [install|update|restart|status|logs]"; exit 1 ;;
esac

while true; do
    menu
    read -rp "انتخاب: " choice
    case "$choice" in
        1)  do_install; pause ;;
        2)  do_update; pause ;;
        3)  systemctl restart "$SERVICE_NAME"; sleep 2
            systemctl is-active --quiet "$SERVICE_NAME" && ok "ریستارت شد" || err "بالا نیومد"
            pause ;;
        4)  do_status; pause ;;
        5)  do_logs ;;
        6)  do_errors; pause ;;
        7)  do_env; pause ;;
        8)  do_ytdlp; pause ;;
        9)  do_botapi; pause ;;
        10) do_instagram; pause ;;
        11) systemctl stop "$SERVICE_NAME"; ok "بات خاموش شد"; pause ;;
        12) do_clearcache; pause ;;
        13) do_audioformat; pause ;;
        14) do_diag; pause ;;
        15) do_fixperms; pause ;;
        16) do_botapi_repair; pause ;;
        17) do_reclog; pause ;;
        18) do_spotify; pause ;;
        19) do_channel; pause ;;
        20) do_igcheck; pause ;;
        21) do_engines; pause ;;
        22) do_igdirect; pause ;;
        23) do_whisper; pause ;;
        24) do_deps; pause ;;
        25) do_shazamtest; pause ;;
        26) do_igreset; pause ;;
        27) do_proxy; pause ;;
        28) do_igtest; pause ;;
        29) do_igwatch; pause ;;
        30) do_igmqtt; pause ;;
        31) do_reset; pause ;;
        0)  echo; exit 0 ;;
        *)  err "گزینه نامعتبر"; sleep 1 ;;
    esac
done
