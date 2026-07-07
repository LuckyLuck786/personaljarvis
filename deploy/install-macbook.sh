#!/usr/bin/env bash
# ============================================================
# JARVIS MacBook setup — the opportunistic inference node.
# Phase 0 scope (truthful): this node only needs to (a) run Ollama,
# (b) be reachable from the hub over the tailnet. Capture collectors
# (Phase 2) and a launchd agent will be added by later phases.
# Run:  bash deploy/install-macbook.sh
# ============================================================
set -euo pipefail

echo "==> checking Ollama"
if ! command -v ollama >/dev/null; then
  echo "Ollama not installed. Install from https://ollama.com/download then re-run."
  exit 1
fi

echo "==> pulling default models (see config/routing.yaml)"
ollama pull qwen2.5:14b || echo "  (skip/fail is OK — edit routing.yaml to match what you pull)"
ollama pull llama3.1:8b || true

echo "==> exposing Ollama beyond loopback (required for the hub to reach it)"
echo "    Ollama binds 127.0.0.1 by default. Make it listen on all interfaces"
echo "    (the tailnet firewall is your perimeter):"
echo "        launchctl setenv OLLAMA_HOST 0.0.0.0"
echo "    then restart the Ollama app. Verify from the hub:"
echo "        curl http://<macbook-tailnet-name>:11434/api/tags"

echo "==> checking Tailscale"
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
  echo "    tailscale is up: $(tailscale ip -4 2>/dev/null | head -1)"
else
  echo "    Tailscale not running. Install from https://tailscale.com/download"
  echo "    and join the same tailnet as the hub."
fi

echo "==> done. Set nodes.macbook.ollama_url in the hub's config/jarvis.yaml"
echo "    to this machine's tailnet address."
