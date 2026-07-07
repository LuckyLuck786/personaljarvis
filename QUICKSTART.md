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
