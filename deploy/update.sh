#!/usr/bin/env bash
# One-command update: pull latest code from GitHub, install deps, restart.
#
#   bash /opt/telegram_downloader_bot/deploy/update.sh
#
# .env, cookies and downloads are git-ignored, so a pull never touches them.

set -euo pipefail

PROJECT_DIR="/opt/telegram_downloader_bot"
SERVICE_NAME="tg-downloader-bot"
BOT_USER="botuser"

if [[ "$EUID" -ne 0 ]]; then
    echo "Please run as root."
    exit 1
fi

echo "==> Pulling latest code..."
sudo -u "$BOT_USER" git -C "$PROJECT_DIR" fetch origin
sudo -u "$BOT_USER" git -C "$PROJECT_DIR" reset --hard origin/main

echo "==> Syntax check..."
"$PROJECT_DIR/.venv/bin/python" -m py_compile \
    "$PROJECT_DIR"/main.py "$PROJECT_DIR"/config.py \
    "$PROJECT_DIR"/modules/*.py "$PROJECT_DIR"/handlers/*.py "$PROJECT_DIR"/utils/*.py

echo "==> Updating dependencies..."
sudo -u "$BOT_USER" "$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

echo "==> Restarting service..."
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE_NAME"
