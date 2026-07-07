# Deploying JARVIS — step by step

This gets JARVIS running for real across your two machines:

- **hub** — the Ubuntu Mac Mini (always on, 6 GB RAM)
- **macbook** — your MacBook Pro (heavy inference + capture + voice, when awake)

Total time: ~30–45 minutes, most of it model downloads. Do the steps in
order. Every command is copy-paste-able; `$` lines run in a terminal.

---

## Overview of what you'll do

1. Build a private network (Tailscale) joining the Mini, the MacBook, and your phone.
2. Install Ollama + models on both machines.
3. Install the JARVIS hub on the Mini (systemd, one script).
4. Point the hub at the MacBook and create your Telegram bot.
5. Turn on capture, voice, and the dashboard.
6. Verify each piece.

---

## Step 1 — Private network (Tailscale)

Do this first; everything else uses the tailnet instead of exposing ports.

**On the Mini (hub):**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # note this address, e.g. 100.x.y.z  → "HUB_IP"
tailscale status         # note the Mini's magic-DNS name, e.g. "mini"
```

**On the MacBook:** install the Tailscale app (App Store or
https://tailscale.com/download), sign in with the **same account**.
```bash
tailscale ip -4          # note this  → "MACBOOK_IP"; magic-DNS name e.g. "macbook"
```

**On your phone:** install the Tailscale app, sign in with the same account.
(That's what lets the Telegram bot and dashboard reach the hub from anywhere,
with no public ports.)

Verify: from the Mini, `ping macbook` should work.

---

## Step 2 — Ollama + models

**On the MacBook (primary inference):**
```bash
# install Ollama from https://ollama.com/download, then:
launchctl setenv OLLAMA_HOST 0.0.0.0     # let the hub reach it over the tailnet
# restart the Ollama app so it binds all interfaces
ollama pull qwen2.5:14b                    # main chat/reasoning model
ollama pull llama3.1:8b                    # faster model
```
Verify from the **Mini**: `curl http://macbook:11434/api/tags` lists the models.

**On the Mini (hub — embeddings + small fallback only):**
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text               # embeddings (the always-on job)
ollama pull qwen2.5:3b                      # small fallback (good at tools)
```
> The Mini only ever runs the ~300 MB embedder and, as a last resort, the 3B
> model on demand. Neither is pinned in RAM.

---

## Step 3 — Install the hub (Mini)

```bash
git clone https://github.com/LuckyLuck786/personaljarvis.git
cd personaljarvis
sudo bash deploy/install-hub.sh
```

This creates the `jarvis` system user, installs to `/opt/jarvis`, generates
your secrets into `/etc/jarvis/jarvis.env` (mode 640), installs+starts the
`jarvis-hub` systemd service (capped at `MemoryMax=1G`), and verifies health.

**Back up your master key now** (losing it = losing encrypted memory):
```bash
sudo grep JARVIS_MASTER_KEY /etc/jarvis/jarvis.env   # copy this somewhere safe, OFF the Mini
```

---

## Step 4 — Point the hub at the MacBook + configure

Edit `/opt/jarvis/config/jarvis.yaml` on the Mini:
```yaml
hub:
  bind_host: 0.0.0.0        # bind the tailnet interface (Tailscale is your firewall)

nodes:
  macbook:
    ollama_url: http://macbook:11434     # the MacBook's magic-DNS name
  hub_ollama:
    ollama_url: http://127.0.0.1:11434   # Ollama on the Mini itself
```

Then restart: `sudo systemctl restart jarvis-hub`.

Check it: `sudo -u jarvis JARVIS_DATA_DIR=/var/lib/jarvis \
/opt/jarvis/.venv/bin/jarvis status` — both nodes should read `up`.

---

## Step 5 — Telegram bot (your primary interface)

1. On Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Message **@userinfobot** → copy your numeric **user ID**.
3. On the Mini, add both to the env file:
   ```bash
   sudo nano /etc/jarvis/jarvis.env
   #   TELEGRAM_BOT_TOKEN=123456:ABC...
   #   TELEGRAM_ALLOWED_USER_IDS=<your numeric id>
   ```
4. (Optional) add cloud fallback keys in the same file: `GROQ_API_KEY=…`,
   `GEMINI_API_KEY=…`, `CEREBRAS_API_KEY=…`.
5. `sudo systemctl restart jarvis-hub`

Now message your bot on Telegram: "Remember I keep the backups on the NAS."
Then later: "Where do I keep backups?" — it should recall it. `/status`,
`/pause`, `/resume` also work. Anyone not on your allow-list gets silence.

---

## Step 6 — Turn on the extras

**Capture (on the MacBook — where you work):**
```bash
# on the MacBook, clone the repo and set up the venv:
git clone https://github.com/LuckyLuck786/personaljarvis.git && cd personaljarvis
python3 -m venv .venv && .venv/bin/pip install -e .
# point it at the hub + your API key:
echo "JARVIS_HUB_URL=http://mini:8700"          >> .env
echo "JARVIS_API_KEY=<the key from /etc/jarvis/jarvis.env on the Mini>" >> .env
# choose sources in config/capture.yaml (flip enabled: true), then:
.venv/bin/jarvis capture run          # or install the launchd agent (deploy/launchd/)
```

**Dashboard:** browse to `http://mini:8700/` from any device on the tailnet,
paste your API key once.

**Voice (on the MacBook):**
```bash
.venv/bin/pip install -e ".[voice]"    # STT + wake word + mic
# install Piper (TTS) from https://github.com/rhasspy/piper, grab a voice .onnx
.venv/bin/jarvis voice-check
.venv/bin/jarvis voice --tts-voice ~/piper/en_GB-alan-medium.onnx
#   say "Jarvis, what's on my plate today?"
```

**Proactive engine** is already running on the hub — it will send you a
morning digest at 07:30 and an evening one at 20:30 on Telegram, nudge
follow-ups, alert on node-down, and consolidate memory + rebuild your digital
twin nightly. Trigger one now to see it: on the Mini,
`… jarvis digest morning`.

---

## Verify the whole system

On the Mini (or any tailnet machine with the CLI + API key):
```bash
jarvis status            # hub ok, both nodes up, cloud tiers as configured
jarvis audit verify      # audit chain intact
jarvis jobs              # scheduled proactive jobs with next-run times
```
Health also at `http://mini:8700/health` (with the `x-api-key` header) and
visually on the dashboard.

---

## Day-to-day

| You want to… | Do this |
|---|---|
| Talk to it | Telegram, or `jarvis chat "…"`, or `jarvis voice` |
| Store / recall | "Remember …" / "What did I say about …" (or `jarvis remember` / `jarvis recall`) |
| See your activity | `jarvis timeline` or the dashboard |
| Tasks / reminders | "remind me at 6pm to …", `jarvis chat "what are my tasks?"` |
| Stop everything now | `jarvis pause` (or the dashboard's Pause button) |
| Resume | `jarvis resume` |
| See what it did | `jarvis audit tail` |
| Automation ideas | `jarvis proposals` |
| Its model of you | `jarvis twin` |

---

## Troubleshooting

- **`jarvis status` can't reach the hub** → `sudo systemctl status jarvis-hub`
  and `journalctl -u jarvis-hub -n 50` on the Mini.
- **A node shows `down`** → is Ollama running there? For the MacBook, is
  `OLLAMA_HOST=0.0.0.0` set and the app restarted? `curl http://macbook:11434/api/tags`
  from the Mini.
- **Telegram silent** → confirm your numeric ID is in `TELEGRAM_ALLOWED_USER_IDS`
  and you restarted the service. Unauthorized senders are ignored by design.
- **Chat says "degraded mode"** → no inference tier reachable (MacBook asleep +
  no cloud keys). Add a cloud key or wake the MacBook; embeddings/memory still work.
- **Tool picked wrong on the 3B model** → phrase explicitly, or add a cloud
  key / keep the MacBook awake for the larger model (see README caveat).
