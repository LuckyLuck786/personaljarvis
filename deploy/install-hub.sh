#!/usr/bin/env bash
# ============================================================
# JARVIS hub installer — Ubuntu Mac Mini (the always-on node).
# Lean-by-design: native systemd, no Docker, ~60-70 MB daemon RSS.
# Run from the repo root:  sudo bash deploy/install-hub.sh
# Idempotent: safe to re-run for upgrades.
# ============================================================
set -euo pipefail

APP_DIR=/opt/jarvis
DATA_DIR=/var/lib/jarvis
ENV_DIR=/etc/jarvis
ENV_FILE=$ENV_DIR/jarvis.env
REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
command -v python3 >/dev/null || { echo "python3 missing: apt install python3 python3-venv"; exit 1; }
python3 - <<'EOF' || { echo "python >= 3.11 required (apt install python3.11-venv or newer)"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

echo "==> service user + directories"
id -u jarvis &>/dev/null || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin jarvis
mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"

echo "==> sync code to $APP_DIR"
# Exclude operator-local overrides so `*.local.yaml` in $APP_DIR/config
# survives a reinstall (rsync --delete would otherwise remove them).
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '.env' \
  --exclude '*.local.yaml' \
  "$REPO_DIR/" "$APP_DIR/"

echo "==> python venv + install"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet "$APP_DIR"
# Drop LanceDB/pyarrow if a previous install left them: their native kernels
# require AVX and SIGILL on pre-2011 CPUs (2010 Mac Mini). Vectors now live in
# SQLite; nothing imports these anymore.
"$APP_DIR/.venv/bin/pip" uninstall -y lancedb pyarrow >/dev/null 2>&1 || true

echo "==> secrets ($ENV_FILE)"
if [[ ! -f $ENV_FILE ]]; then
  cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
( cd "$ENV_DIR" && "$APP_DIR/.venv/bin/jarvis" keygen --env-file "$ENV_FILE" )
chown root:jarvis "$ENV_FILE" && chmod 640 "$ENV_FILE"
chown -R jarvis:jarvis "$DATA_DIR"

echo "==> systemd unit"
cp "$APP_DIR/deploy/systemd/jarvis-hub.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now jarvis-hub

echo "==> waiting for health..."
echo "==> hub Ollama models (embeddings + always-on small chat fallback)"
# These must match config/routing.yaml's hub_ollama tier. Without the chat
# model the hub degrades to "no inference tier" whenever the MacBook sleeps.
HUB_EMBED_MODEL=nomic-embed-text
HUB_CHAT_MODEL=qwen2.5:3b
if command -v ollama >/dev/null; then
  ollama pull "$HUB_EMBED_MODEL" || echo "  ! pull $HUB_EMBED_MODEL manually"
  ollama pull "$HUB_CHAT_MODEL"  || echo "  ! pull $HUB_CHAT_MODEL manually"
else
  echo "  Ollama not installed on the hub. Install + pull the hub models:"
  echo "    curl -fsSL https://ollama.com/install.sh | sh"
  echo "    ollama pull $HUB_EMBED_MODEL && ollama pull $HUB_CHAT_MODEL"
fi

sleep 3
API_KEY=$(grep '^JARVIS_API_KEY=' "$ENV_FILE" | cut -d= -f2)
if curl -sf -H "x-api-key: $API_KEY" http://127.0.0.1:8700/health >/dev/null; then
  echo "==> jarvis-hub is UP. Try:"
  echo "    sudo systemctl status jarvis-hub"
  echo "    curl -H \"x-api-key: \$KEY\" http://127.0.0.1:8700/health | jq"
else
  echo "==> health check FAILED — inspect: journalctl -u jarvis-hub -n 50" >&2
  exit 1
fi

cat <<'EON'

Next steps (manual, one-time):
  1. Network: install Tailscale (https://tailscale.com/download/linux) and
     join the Mini, the MacBook, and your phone to one tailnet. Keep the hub
     bound to 127.0.0.1 / the tailscale IP — never a public interface.
  2. Edit /opt/jarvis/config/jarvis.yaml: set nodes.macbook.ollama_url to
     the MacBook's tailnet hostname, and pull qwen2.5:14b on the MacBook.
  3. This installer already pulled the hub models (nomic-embed-text +
     qwen2.5:3b). Verify with `ollama list`. They MUST match
     config/routing.yaml or chat degrades when the MacBook is asleep.
  4. Run `jarvis doctor` to confirm everything is wired before relying on it.
  5. Back up JARVIS_MASTER_KEY from /etc/jarvis/jarvis.env somewhere off-hub.
EON
