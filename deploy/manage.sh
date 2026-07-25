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
        ffmpeg git unzip curl ca-certificates 2>/dev/null \
      || apt-get install -y python3 python3-pip "python${pyver}-venv" ffmpeg git unzip curl ca-certificates \
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
    [[ -f "$PROJECT_DIR/.env" ]] && chmod 600 "$PROJECT_DIR/.env"
    git config --global --add safe.directory "$PROJECT_DIR" 2>/dev/null
}

syntax_check() {
    "$PROJECT_DIR/.venv/bin/python" -m py_compile \
        "$PROJECT_DIR"/main.py "$PROJECT_DIR"/config.py \
        "$PROJECT_DIR"/modules/*.py "$PROJECT_DIR"/handlers/*.py "$PROJECT_DIR"/utils/*.py
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

    docker rm -f telegram-bot-api 2>/dev/null
    docker run -d --name telegram-bot-api --restart always \
        -p 127.0.0.1:8081:8081 \
        -e TELEGRAM_API_ID="$api_id" \
        -e TELEGRAM_API_HASH="$api_hash" \
        -v telegram-bot-api-data:/var/lib/telegram-bot-api \
        aiogram/telegram-bot-api:latest --local || { err "docker بالا نیومد"; return; }

    sed -i 's|^BOT_API_BASE_URL=.*|BOT_API_BASE_URL=http://127.0.0.1:8081|' "$PROJECT_DIR/.env"
    sed -i 's|^MAX_UPLOAD_MB=.*|MAX_UPLOAD_MB=2000|' "$PROJECT_DIR/.env"

    sleep 5
    systemctl restart "$SERVICE_NAME"
    ok "فعال شد - حالا تا ۲ گیگ آپلود می‌شه"
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

install_shortcut() {
    ln -sf "$PROJECT_DIR/deploy/manage.sh" /usr/local/bin/botctl
    chmod +x "$PROJECT_DIR/deploy/manage.sh" 2>/dev/null
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
    echo " 12) نصب مجدد از صفر (پاک کردن همه چی)"
    echo "  0) خروج"
    echo
}

# Non-interactive shortcuts:  botctl install | update | restart | status | logs
case "${1:-}" in
    install) do_install; exit $? ;;
    reset)   do_reset;   exit $? ;;
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
        12) do_reset; pause ;;
        0)  echo; exit 0 ;;
        *)  err "گزینه نامعتبر"; sleep 1 ;;
    esac
done
