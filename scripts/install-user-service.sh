#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="crawl4ai-mcp.service"
UNIT_SRC="$PROJECT_DIR/systemd/$UNIT_NAME"
USER_UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DEST="$USER_UNIT_DIR/$UNIT_NAME"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "error: $PROJECT_DIR/.env is missing; create it first (see .env.example)" >&2
    exit 1
fi
mode="$(stat -c '%a' "$PROJECT_DIR/.env")"
if [[ "$mode" != "600" ]]; then
    echo "error: .env must have mode 600, got $mode; run: chmod 600 $PROJECT_DIR/.env" >&2
    exit 1
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/crawl4ai-mcp" ]]; then
    echo "error: $PROJECT_DIR/.venv/bin/crawl4ai-mcp not found; install the package first" >&2
    exit 1
fi

mkdir -p "$USER_UNIT_DIR"
sed -e "s|%h/Workspace/crawl4ai-mcp|$PROJECT_DIR|g" "$UNIT_SRC" > "$UNIT_DEST"
chmod 0644 "$UNIT_DEST"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo "waiting for http://127.0.0.1:11236/health ..."
for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:11236/health >/dev/null 2>&1; then
        echo "service is healthy"
        exit 0
    fi
    sleep 1
done

echo "error: service did not become healthy within 60 seconds" >&2
systemctl --user status --no-pager "$UNIT_NAME" >&2 || true
journalctl --user -u "$UNIT_NAME" -n 40 --no-pager >&2 || true
exit 1
