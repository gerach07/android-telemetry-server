#!/usr/bin/env bash
set -euo pipefail

INSTANCE="${GCE_INSTANCE:-instance-20260604-093230}"
ZONE="${GCE_ZONE:-us-central1-a}"
REMOTE_WEB="/home/kaijakaija88/android-telemetry-server/web"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[*] Packaging web update..."
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Copy files to temp directory
cp "$ROOT/web/main.py" "$TMPDIR/"
cp "$ROOT/web/full_simulator.py" "$TMPDIR/"
cp "$ROOT/web/requirements.txt" "$TMPDIR/"
cp "$ROOT/web/start_commands.txt" "$TMPDIR/"
cp -r "$ROOT/web/templates" "$TMPDIR/"

# Ensure the static directory exists (fixes the uvicorn startup crash)
mkdir -p "$TMPDIR/static"
if [ -d "$ROOT/web/static" ]; then
    cp -r "$ROOT/web/static/"* "$TMPDIR/static/" 2>/dev/null || true
fi

# Package everything including the static folder
tar czf "$TMPDIR/web-update.tar.gz" -C "$TMPDIR" main.py full_simulator.py requirements.txt start_commands.txt templates static

echo "[*] Uploading to $INSTANCE..."
gcloud compute scp "$TMPDIR/web-update.tar.gz" "$INSTANCE:/tmp/web-update.tar.gz" --zone="$ZONE"

echo "[*] Installing and restarting service..."
gcloud compute ssh "$INSTANCE" --zone="$ZONE" --command="
  sudo bash -c '
    mkdir -p \"$REMOTE_WEB\" &&
    tar xzf /tmp/web-update.tar.gz -C \"$REMOTE_WEB\" &&
    chown -R kaijakaija88:kaijakaija88 \"$REMOTE_WEB\" &&
    systemctl restart android-telemetry-server &&
    sleep 2 &&
    systemctl is-active android-telemetry-server &&
    rm -f /tmp/web-update.tar.gz
  '
"

echo "[*] Server deployed to $INSTANCE ($ZONE)"