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
        ffmpeg git unzip curl ca-certificates libchromaprint-tools 2>/dev/null \
      || apt-get install -y python3 python3-pip "python${pyver}-venv" ffmpeg git unzip curl ca-certificates libchromaprint-tools \
      || warn "بعضی پکیج‌ها نصب نشدن - ادامه می‌دم و venv رو چک می‌کنم"

    # Verify venv actually works now, otherwise stop before we build a broken one.
    if ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
        err "ماژول venv هنوز کار نمی‌کنه. دستی بزن:  apt install -y python${pyver}-venv"
        return 1
    fi
    ok "پکیج‌ها نصب شدن"
}

ensure_deno() {
    if command -v deno &>/dev/null; then
        ok "Deno از قبل هست: $(deno --version | head -1)"
        return
    fi
    info "نصب Deno (برای yt-dlp لازمه)..."
    curl -fsSL -o /tmp/deno.zip \
        https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
    unzip -oq /tmp/deno.zip -d /usr/local/bin
    chmod +x /usr/local/bin/deno
    rm -f /tmp/deno.zip
    ok "Deno نصب شد: $(deno --version | head -1)"
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
        read -rp "بازم ریستارت کنم؟ (y/n) " a
        [[ "$a" == "y" ]] && { systemctl restart "$SERVICE_NAME"; ok "ریستارت شد"; }
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
    command -v deno &>/dev/null && ok "Deno: $(deno --version | head -1)" || err "Deno نصب نیست"
    [[ -x "$PROJECT_DIR/.venv/bin/python" ]] && \
        ok "yt-dlp: $("$PROJECT_DIR/.venv/bin/python" -m yt_dlp --version 2>/dev/null || echo '?')"
}

do_logs() {
    echo; info "لاگ زنده - برای خروج Ctrl+C بزن"; echo
    journalctl -u "$SERVICE_NAME" -f -n 40
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

do_ytdlp() {
    echo; info "آپدیت yt-dlp (وقتی یوتیوب خراب می‌شه اینو بزن)..."
    sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --upgrade yt-dlp
    ok "نسخه جدید: $("$PROJECT_DIR/.venv/bin/python" -m yt_dlp --version)"
    ensure_deno
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

    local dm_user dm_pass
    read -rp  "یوزرنیم اکانت دایرکت: " dm_user
    read -rsp "پسورد اکانت دایرکت: " dm_pass; echo
    [[ -z "$dm_user" || -z "$dm_pass" ]] && { err "یوزرنیم و پسورد لازمه"; return 1; }

    set_env IG_DM_USERNAME "$dm_user"
    set_env IG_DM_PASSWORD "$dm_pass"
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

    if (( want_official && want_standby )); then
        set_env IG_DIRECT_SOURCES "webhook,poll"
    elif (( want_official )); then
        set_env IG_DIRECT_SOURCES "webhook"
    elif (( want_standby )); then
        set_env IG_DIRECT_SOURCES "poll"
    else
        err "هیچ مسیری تنظیم نشد"
        return 1
    fi

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

do_deps() {
    echo; info "=== وضعیت پکیج‌ها ==="; echo

    local py="$PROJECT_DIR/.venv/bin/python"
    local pip="$PROJECT_DIR/.venv/bin/pip"

    info "نسخه‌ها:"
    "$pip" list 2>/dev/null | grep -iE '^(shazamio|shazamio-core|pydantic|pydantic-core|aiohttp|instagrapi|faster-whisper|ctranslate2|yt-dlp|python-telegram-bot|httpx)\b' \
        || warn "pip list جواب نداد"
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
    echo " 25) نصب مجدد از صفر (پاک کردن همه چی)"
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
    engines) do_engines; exit $? ;;
    update)  do_update;  exit $? ;;
    restart) systemctl restart "$SERVICE_NAME"; exit $? ;;
    status)  do_status;  exit 0 ;;
    logs)    do_logs;    exit 0 ;;
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
        25) do_reset; pause ;;
        0)  echo; exit 0 ;;
        *)  err "گزینه نامعتبر"; sleep 1 ;;
    esac
done
