#!/bin/bash
# Setup hatani user for agent sandbox execution (no Docker)
# Run ONCE on the host: sudo ./setup_sandbox_user.sh
set -euo pipefail

SANDBOX_USER="hatani"

echo "=== Setting up sandbox user: $SANDBOX_USER ==="

# Create system user if not exists
if id "$SANDBOX_USER" &>/dev/null; then
    echo "[OK] User '$SANDBOX_USER' exists (uid=$(id -u "$SANDBOX_USER"))"
else
    useradd -r -m -s /bin/bash "$SANDBOX_USER"
    echo "[OK] Created '$SANDBOX_USER' (uid=$(id -u "$SANDBOX_USER"))"
fi

# Install packages system-wide
echo "=== Installing packages ==="
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-pip bash coreutils curl wget \
    git jq zip unzip ffmpeg

pip3 install --break-system-packages --no-cache-dir \
    numpy pandas matplotlib pillow scipy sympy \
    requests httpx aiohttp beautifulsoup4 lxml \
    openpyxl pydub pyyaml python-dotenv qrcode

# Sudoers: allow any user to run as hatani without password
SUDOERS_FILE="/etc/sudoers.d/hatani"
if [ ! -f "$SUDOERS_FILE" ]; then
    echo "ALL ALL=($SANDBOX_USER) NOPASSWD: ALL" > "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    echo "[OK] Sudoers: $SUDOERS_FILE"
fi

# Workspace dir
WS_DIR="$(dirname "$0")/agent/.agent_workspaces"
mkdir -p "$WS_DIR"
chown "$SANDBOX_USER:$SANDBOX_USER" "$WS_DIR" 2>/dev/null || true

# Test
sudo -u "$SANDBOX_USER" python3 -c "import numpy; print('OK: numpy', numpy.__version__)"
echo "=== Done ==="
