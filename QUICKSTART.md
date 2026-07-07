# QUICKSTART

Goal: clone → talking to JARVIS, in the fewest steps.

## Dev machine (any Mac/Linux, Python ≥ 3.11)

```bash
git clone https://github.com/LuckyLuck786/personaljarvis.git jarvis && cd jarvis
uv venv && uv pip install -e ".[dev]"     # or: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/jarvis keygen                   # generates API + master keys into .env (chmod 600)
.venv/bin/jarvis migrate                  # creates ~/.jarvis/jarvis.db
.venv/bin/jarvis serve &                  # hub on http://127.0.0.1:8700
.venv/bin/jarvis status
```

Expected output (Ollama not running, no cloud keys — honestly `degraded`):

```
JARVIS hub v0.1.0  status=degraded  killswitch=active
  hub: db=ok rss=61.8MB disk_free=73.7GB bus_backlog=0 uptime=2.8s
  node macbook: down (ConnectError)
  node hub_ollama: down (ConnectError)
  cloud: groq=not_configured, cerebras=not_configured, gemini=not_configured
```

Start Ollama and the `macbook` node flips to `up` within its 30 s probe
interval. Run the tests: `.venv/bin/python -m pytest`.

## Talk to it (CLI)

```bash
ollama pull nomic-embed-text && ollama pull llama3.2:3b   # embeddings + small chat model
.venv/bin/jarvis chat "Remember: I decided on the Charter serif font for my resume."
.venv/bin/jarvis chat "What did I decide about my resume font?"
#   → "You decided on a serif font, specifically Charter, for your resume design, sir."
.venv/bin/jarvis recall "resume font"     # raw hybrid memory search
```

Watch which tier served each request: `.venv/bin/jarvis bus tail llm.request`.

## Talk to it (Telegram — the point of the exercise)

1. Message **@BotFather** on Telegram → `/newbot` → copy the token.
2. Message **@userinfobot** → copy your numeric user ID.
3. In `.env` (or `/etc/jarvis/jarvis.env` on the hub):
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_ALLOWED_USER_IDS=<your numeric id>
   ```
4. Restart the hub (`sudo systemctl restart jarvis-hub` or re-run `jarvis serve`).
5. Message your bot. `/status`, `/pause`, `/resume` work; anything else is
   conversation. Anyone not on the allow-list gets silence (and an audit entry).

## Give it tools (Phase 3)

Tools are on by default. Talk naturally:

```bash
.venv/bin/jarvis chat "remind me at 18:30 to call the plumber"
.venv/bin/jarvis chat "what are my open tasks?"
.venv/bin/jarvis chat "run df -h and tell me the disk usage"     # allow-listed shell
.venv/bin/jarvis chat "fetch example.com and summarize it"
```

Destructive actions require confirmation:

```bash
.venv/bin/jarvis chat "reboot the mini node"
#   → "Reply `confirm ab12cd` to run homelab_reboot({"node": "mini"})…"
.venv/bin/jarvis chat "confirm ab12cd"
```

Configure calendar (`tools.calendar_ics_urls` in `config/jarvis.yaml`),
email (`IMAP_*` in `.env`), home-lab nodes (`tools.homelab`), and the shell
allow-list (`config/permissions.yaml`) to light up those tools. On the hub's
3B model, phrase tool requests explicitly; the MacBook/cloud tiers are more
forgiving (see README caveat).

## Talk to it out loud (Phase 5, on the MacBook)

```bash
pip install -e ".[voice]"                      # faster-whisper, openWakeWord, sounddevice
# Piper (TTS) is a separate binary: https://github.com/rhasspy/piper
.venv/bin/jarvis voice-check                    # honest report of what's enabled
.venv/bin/jarvis voice --tts-voice ~/piper/en_GB-alan-medium.onnx
#   say "Jarvis, what's on my plate today?" → spoken answer
```

Without the extras or a mic it falls back to push-to-talk / typed input —
still the full cognition pipeline, just no audio. Verified working in text
mode: `echo "what is 17 times 3?" | jarvis voice` → "17 times 3 equals 51."

## Capture what you do (Phase 2)

Collectors are opt-in. Edit `config/capture.yaml`, flip `enabled: true` on
the sources you want (notes, clipboard, filesystem, shell_history,
browser_history), then on the machine you work on:

```bash
.venv/bin/jarvis capture run          # foreground; Ctrl-C to stop
# MacBook, permanent: see deploy/launchd/com.jarvis.capture.plist
```

Collectors redact secrets (regexes in `capture.yaml`) *before* anything
leaves the collector, forward to the hub's authenticated API, baseline on
first run (no years-of-history flood), and re-emit if the hub is down.

```bash
.venv/bin/jarvis timeline --hours 24                 # what happened today
.venv/bin/jarvis chat "what was I working on today?" # ask instead
```

## The hub (Ubuntu Mac Mini, production)

```bash
git clone https://github.com/LuckyLuck786/personaljarvis.git && cd personaljarvis
sudo bash deploy/install-hub.sh
```

That installs to `/opt/jarvis`, creates the `jarvis` system user, writes
secrets to `/etc/jarvis/jarvis.env`, installs + starts the systemd unit
(`MemoryMax=1G`), and verifies health. Then:

1. Install Tailscale on Mini + MacBook + phone; keep the hub off public
   interfaces.
2. Point `nodes.macbook.ollama_url` in `/opt/jarvis/config/jarvis.yaml` at
   the MacBook's tailnet name; `sudo systemctl restart jarvis-hub`.
3. **Back up `JARVIS_MASTER_KEY`** (from `/etc/jarvis/jarvis.env`) off-hub.

## The MacBook (inference node)

```bash
bash deploy/install-macbook.sh   # checks Ollama + Tailscale, pulls models,
                                 # prints the OLLAMA_HOST exposure step
```

## Phase 0 demo script

```bash
K=$(grep '^JARVIS_API_KEY=' .env | cut -d= -f2)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8700/health   # 401 — no key, no entry
.venv/bin/jarvis status                                                 # authenticated health
.venv/bin/jarvis pause --reason demo                                    # kill switch
.venv/bin/jarvis bus publish x '{"a":1}'                                # → 503 while paused
.venv/bin/jarvis resume
.venv/bin/jarvis bus publish demo.topic '{"hello":"world"}'
.venv/bin/jarvis bus tail                                               # replayable message log
.venv/bin/jarvis audit tail                                             # pause/resume were audited
.venv/bin/jarvis audit verify                                           # hash chain intact
```
