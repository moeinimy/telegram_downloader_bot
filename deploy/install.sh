#!/usr/bin/env bash
# Fresh-server installer for Ubuntu 24 (noble). Run as root.
#
# Expected flow (git-based deploy):
#   git clone <repo-url> /opt/telegram_downloader_bot
#   cd /opt/telegram_downloader_bot
#   cp .env.example .env   (then fill in real values)
#   bash deploy/install.sh
#
# What it does:
#   1. Installs system deps (python3, venv, ffmpeg, git, unzip).
#   2. Installs Deno (needed by yt-dlp for YouTube EJS challenges).
#   3. Creates a dedicated unprivileged user 'botuser'.
#   4. Builds a venv and installs requirements.
#   5. Installs & enables the systemd service.
#
# Later updates: just run  bash deploy/update.sh

set -euo pipefail

PROJECT_DIR="/opt/telegram_downloader_bot"
SERVICE_NAME="tg-downloader-bot"
BOT_USER="botuser"

if [[ "$EUID" -ne 0 ]]; then
    echo "Please run as root (sudo bash deploy/install.sh)"
    exit 1
fi

if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    echo "ERROR: $PROJECT_DIR is not a git clone."
    echo "Run:  git clone <repo-url> $PROJECT_DIR   first."
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "ERROR: $PROJECT_DIR/.env not found."
    echo "Run:  cp $PROJECT_DIR/.env.example $PROJECT_DIR/.env   and fill it first."
    exit 1
fi

echo "==> Installing system packages..."
apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg git unzip curl ca-certificates

if ! command -v deno &>/dev/null; then
    echo "==> Installing Deno (JS runtime for yt-dlp)..."
    curl -fsSL -o /tmp/deno.zip \
        https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
    unzip -o /tmp/deno.zip -d /usr/local/bin
    chmod +x /usr/local/bin/deno
    rm -f /tmp/deno.zip
fi
echo "    deno: $(deno --version | head -1)"

echo "==> Creating user '$BOT_USER' (if missing)..."
id -u "$BOT_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$BOT_USER"

echo "==> Setting ownership..."
mkdir -p "$PROJECT_DIR/downloads"
chown -R "$BOT_USER:$BOT_USER" "$PROJECT_DIR"
chmod 600 "$PROJECT_DIR/.env"

echo "==> Building Python venv..."
sudo -u "$BOT_USER" python3 -m venv "$PROJECT_DIR/.venv"
sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "==> Installing systemd service..."
cp "$PROJECT_DIR/deploy/$SERVICE_NAME.service" "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
systemctl --no-pager status "$SERVICE_NAME" || true

echo ""
echo "Done. Useful commands:"
echo "   bash $PROJECT_DIR/deploy/update.sh      # pull latest code + restart"
echo "   systemctl status $SERVICE_NAME"
echo "   journalctl -u $SERVICE_NAME -f"
