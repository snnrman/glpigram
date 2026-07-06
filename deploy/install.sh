#!/usr/bin/env bash
# Install / update the GLPI Telegram bot as a systemd service.
# Run as root on the GLPI host:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/glpi-tgbot
DATA_DIR=/var/lib/glpi-tgbot
VENV="$APP_DIR/venv"
SERVICE=glpi-tgbot
USER=glpibot
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo ">> Creating service user $USER (if missing)"
id -u "$USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$USER"

echo ">> Creating directories"
mkdir -p "$APP_DIR" "$DATA_DIR"

echo ">> Syncing application code to $APP_DIR"
# When the repo already lives at $APP_DIR (in-place install) source and target
# are the same directory, so copying would fail ("are the same file"). Skip it.
if [ "$(cd "$SRC_DIR" && pwd -P)" = "$(cd "$APP_DIR" && pwd -P)" ]; then
    echo "   source == target ($APP_DIR); code already in place, skipping copy"
else
    # Copy the bot package and metadata; exclude local dev artefacts.
    cp -r "$SRC_DIR/bot" "$APP_DIR/"
    cp "$SRC_DIR/requirements.txt" "$SRC_DIR/pyproject.toml" "$APP_DIR/"
fi

echo ">> Creating/updating virtualenv"
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ ! -f "$DATA_DIR/.env" ]; then
    echo ">> Installing .env template (EDIT IT with real secrets before starting!)"
    cp "$SRC_DIR/.env.example" "$DATA_DIR/.env"
    chmod 600 "$DATA_DIR/.env"
fi

echo ">> Fixing ownership"
chown -R "$USER:$USER" "$APP_DIR" "$DATA_DIR"

echo ">> Installing systemd unit"
cp "$SRC_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo
echo "Done. Next steps:"
echo "  1. Edit $DATA_DIR/.env with real tokens"
echo "  2. sudo systemctl restart $SERVICE"
echo "  3. journalctl -u $SERVICE -f"
