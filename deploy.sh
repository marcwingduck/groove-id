#!/usr/bin/env bash
# Deploy the groove-id listener to tape and restart it.
#
# The listener imports matrix.py from ~/snapserver-lcd on the same Pi, so after a change
# there, deploy snapserver-lcd first and then this. The fingerprints, tracks.json and
# releases.json are vinyl_indexer.py's job.
#
# Usage:
#   ./deploy.sh                 # uses defaults: pi@tape, /home/pi/vinyl-id
#   ./deploy.sh user@host       # custom host
#   ./deploy.sh user@host /opt/vinyl-id

set -euo pipefail

REMOTE="${1:-pi@tape}"
REMOTE_DIR="${2:-/home/pi/vinyl-id}"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Copying the listener to $REMOTE:$REMOTE_DIR"
ssh "$REMOTE" "mkdir -p '$REMOTE_DIR'"
scp "$LOCAL_DIR/groove_listen.py" "$LOCAL_DIR/store_album.sh" "$LOCAL_DIR/groove-id.service" "$REMOTE:$REMOTE_DIR/"

echo ">>> Installing systemd unit and restarting service"
ssh -t "$REMOTE" "
    set -e
    chmod 755 '$REMOTE_DIR/store_album.sh'
    sudo cp '$REMOTE_DIR/groove-id.service' /etc/systemd/system/groove-id.service
    sudo systemctl daemon-reload
    sudo systemctl enable groove-id.service
    sudo systemctl restart groove-id.service
    sudo systemctl status groove-id.service --no-pager -n 10 || true
"

echo ">>> Done. Tail logs with: ssh $REMOTE 'journalctl -u groove-id -f'"
