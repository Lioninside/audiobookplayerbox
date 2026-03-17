#!/usr/bin/env bash
# ─── Audiobook Player — Install Script ────────────────────────────────────────
# Run once on the Raspberry Pi:  sudo bash install.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="audiobook"
SERVICE_FILE="$SCRIPT_DIR/audiobook.service"
AUDIOBOOKS_DIR="/home/pi/audiobooks"
LOG_FILE="/home/pi/audiobook_player.log"

# ── Must run as root ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Please run as root:  sudo bash install.sh"
    exit 1
fi

echo "=== Audiobook Player — Setup ==="
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/5] Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 \
    python3-pygame \
    python3-rpi.gpio
echo "      Done."

# ── 2. Audiobooks directory ───────────────────────────────────────────────────
echo "[2/5] Creating audiobooks directory: $AUDIOBOOKS_DIR"
mkdir -p "$AUDIOBOOKS_DIR"
chown pi:pi "$AUDIOBOOKS_DIR"
echo "      Done."

# ── 3. Log file permissions ───────────────────────────────────────────────────
echo "[3/5] Setting up log file: $LOG_FILE"
touch "$LOG_FILE"
chown pi:pi "$LOG_FILE"
echo "      Done."

# ── 4. systemd service ────────────────────────────────────────────────────────
echo "[4/5] Installing systemd service…"
if [[ ! -f "$SERVICE_FILE" ]]; then
    echo "      ERROR: $SERVICE_FILE not found!"
    exit 1
fi
cp "$SERVICE_FILE" /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service
echo "      Service enabled (will start on next boot)"
echo "      Done."

# ── 5. Start now ─────────────────────────────────────────────────────────────
echo "[5/5] Starting service…"
systemctl start ${SERVICE_NAME}.service
sleep 2
systemctl status ${SERVICE_NAME}.service --no-pager || true
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════"
echo " Installation complete!"
echo ""
echo " Next steps:"
echo "   1. Copy your MP3 files to $AUDIOBOOKS_DIR"
echo "      Example: scp mybook.mp3 pi@raspberrypi:$AUDIOBOOKS_DIR/"
echo ""
echo "   2. Check GPIO pin numbers in player.py"
echo "      (top of file — PIN_GREEN, PIN_BLUE, etc.)"
echo ""
echo " Useful commands:"
echo "   View live log:     journalctl -u $SERVICE_NAME -f"
echo "   Restart service:   sudo systemctl restart $SERVICE_NAME"
echo "   Stop service:      sudo systemctl stop $SERVICE_NAME"
echo "   Disable autostart: sudo systemctl disable $SERVICE_NAME"
echo "══════════════════════════════════════════════"
