# JARVIS

A private, self-hosted, always-on personal AI assistant. It captures what I do,
remembers it, talks to me (Telegram/voice/CLI), takes actions through gated
tools, and proactively surfaces what matters — running on my own hardware,
with my data encrypted at rest and never leaving the local tier without
explicit opt-in.

## Status — honest phase tracker

| Phase | Scope | Status |
|---|---|---|
| **0 — Foundation** | Repo scaffold, config, secrets, SQLite message bus, migrations, structured logging, auth + rate limiting, hash-chained audit log, kill switch, node health monitoring (MacBook reachability), systemd + installers, tests | ✅ **done, running** |
| **1 — Memory + Chat MVP** | Encrypted RAG (LanceDB + SQLite), hub-local embeddings, tiered model router w/ automatic failover + privacy boundary, memory-grounded chat agent, Telegram bot, CLI chat | ✅ **done, running** — Telegram needs your bot token in `.env` to go live; demo verified over CLI/API with local models |
| **2 — Capture pipeline** | notes / clipboard / filesystem / shell-history / browser-history collectors with at-source redaction, capture API → durable bus → memory ingestion, timeline view + CLI, launchd agent for the MacBook | ✅ **done, running** — all collectors ship `enabled: false` (opt-in in `config/capture.yaml`); screen-OCR is schema-only, honestly unimplemented |
| **3 — Tools & actions** | auto-registering plugin system, agent tool loop (plan→act→observe), permission gating (read/act/destructive/shell) with confirmation flow, reminder firing → Telegram; tools: tasks, reminders, notes, memory-search, web-fetch, allow-listed shell, files (jailed), calendar (ICS), email (IMAP read + draft), home-lab | ✅ **done, running** — see the small-model caveat below |
| 4 — Proactive engine | digests, reminder firing, follow-ups, anomaly alerts, nightly consolidation | ⬜ not started |
| 5 — Voice | openWakeWord + faster-whisper + Piper | ⬜ not started |
| 6 — Web dashboard | timeline, search, task board, logs, routing view, kill switch UI | ⬜ not started |
| 7 — Advanced | self-improvement loop, LoRA personal tuning, multi-agent, digital twin | ⬜ not started |

Anything not marked done does not exist yet beyond config schemas and
placeholder packages — no fabricated capabilities.

## Topology

```
┌────────────────────────────────────────────┐
│ hub — Mac Mini 2015, Ubuntu, 6 GB RAM      │  always on
│  jarvis-hub daemon (FastAPI, systemd)      │
│   ├─ SQLite message bus + structured store │
│   ├─ audit log, kill switch, health/API    │
│   ├─ node monitor (is the MacBook awake?)  │
│   └─ [P1+] embeddings, memory, Telegram,   │
│            proactive engine, dashboard     │
└───────────────┬────────────────────────────┘
        tailscale mesh (no public ports)
┌───────────────┴──────────────┐   ┌─────────────────────────┐
│ macbook — MBP + Ollama       │   │ cloud fallback tiers     │
│  primary inference when awake│   │ Groq / Cerebras / Gemini │
│  [P2+] capture collectors    │   │ non-private tasks only   │
└──────────────────────────────┘   └─────────────────────────┘
```

The hub must keep working when the MacBook sleeps; heavy generation fails
over per `config/routing.yaml`. Everything is host/port-configurable, so the
whole system also runs collapsed onto one dev machine.

## Repo layout

```
jarvis/
  core/        config, logging, db+migrations, bus, crypto, audit, killswitch, security
  hub/         the always-on daemon (FastAPI app, node health monitor)
  cli/         operator CLI (`jarvis …`)
  migrations/  numbered .sql files, applied in order
  memory/ cognition/ router/ capture/ tools/ interfaces/ proactive/   ← later phases
config/        jarvis.yaml (topology), routing.yaml, capture.yaml, permissions.yaml
deploy/        install-hub.sh (Ubuntu/systemd), install-macbook.sh, systemd units
tests/         bus, migrations, security gating, killswitch, audit, crypto, health
```

## Quickstart

See [QUICKSTART.md](QUICKSTART.md). Short version:

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/jarvis keygen      # writes JARVIS_API_KEY / JARVIS_MASTER_KEY to .env (0600)
.venv/bin/jarvis migrate
.venv/bin/jarvis serve       # hub on 127.0.0.1:8700
.venv/bin/jarvis status      # health, node reachability, RAM, killswitch
```

Production hub install (Ubuntu Mac Mini): `sudo bash deploy/install-hub.sh`
— native systemd, no Docker (deliberate: container overhead buys nothing
here and the RAM is precious).

## Hub RAM budget (6 GB machine — measured, not guessed)

| Component | Measured / budgeted |
|---|---|
| jarvis-hub daemon at boot | **~57–62 MB RSS measured** |
| jarvis-hub with RAG pipeline exercised (LanceDB/pyarrow loaded) | **~112 MB RSS measured** |
| systemd cap | `MemoryHigh=512M`, `MemoryMax=1G` — kernel-enforced ceiling |
| SQLite page cache | capped at 8 MB (`PRAGMA cache_size=-8000`) |
| Ollama on hub | load-on-demand (`nomic-embed-text` ~300 MB while embedding, `llama3.2:3b` ~2 GB only while generating); never pinned |
| Redis | **not used** — the bus is SQLite (see decisions) |

## Architecture decisions (running log)

1. **SQLite-backed message bus, no Redis.** Single-operator message rates are
   tiny; an append-only `bus_messages` log + per-consumer cursors gives fanout
   pub/sub, at-least-once delivery, retries with dead-lettering, and free
   replay/debugging — at zero resident RAM. Latency is polling-bounded (~1 s),
   fine for an assistant. `jarvis/core/bus.py`.
2. **One hub process, not a service fleet.** Later phases mount onto the same
   FastAPI daemon. Each extra Python process costs ~30–60 MB base RSS; on
   6 GB, process count is the enemy. Components stay loosely coupled through
   the bus, so they can be split out if the topology ever changes.
3. **Forward-only SQL migrations** wrapped in a transaction *inside*
   `executescript` (which implicitly commits otherwise) so a failed migration
   rolls back atomically. `jarvis/core/db.py`.
4. **Auth on literally every endpoint** including `/health` (constant-time
   API-key compare). Rate limiting runs *before* auth so key-guessing is
   throttled too. The hub refuses to boot with no API key set.
5. **Hash-chained audit log**: each row's SHA-256 covers the previous row's
   hash, so edits *and* deletions are detectable (`jarvis audit verify`).
   Tamper-evident, not tamper-proof — an attacker with DB write access could
   rebuild the chain; the threat model is catching silent corruption and
   casual tampering, with the journald copy as a second witness.
6. **Encryption at rest via app-level Fernet, not SQLCipher.** One master key
   (`JARVIS_MASTER_KEY`, generated by `jarvis keygen`, 0600, backed up
   off-hub), HKDF-derived per-purpose subkeys ("memory", "capture", …).
   Memory/capture *content* columns are encrypted before hitting SQLite or
   the vector payloads (wired to data in Phase 1 — the vault exists and is
   tested now). Recommended base layer underneath: LUKS full-disk encryption
   on the hub. Trade-off documented: SQL can't search inside encrypted
   content; the RAG index works on embeddings + extracted keywords instead.
7. **Kill switch persists in SQLite** and is enforced by middleware: paused ⇒
   everything but `/health*` and `/control/*` returns 503, and (from Phase 1)
   all autonomous components check it before acting. Survives restarts.
8. **Node monitor is the router's eyes.** Background probes of each node's
   Ollama (`/api/tags`) on per-node intervals; results cached (health never
   blocks), persisted with bounded history, and up/down *transitions*
   published to the bus — Phase 4 turns those into anomaly alerts.
9. **No `[standard]` extras on uvicorn**, no docs/openapi endpoints exposed,
   journald owns log retention — small footprint, smaller surface.
10. **Thin provider layer instead of LiteLLM.** We need exactly four API
    shapes (Ollama, OpenAI-compatible ×2, Gemini); ~150 lines replaces a
    large dependency tree and its resident RAM. `jarvis/router/providers.py`.
11. **LanceDB stores vectors + ids only — never text.** All content (docs,
    chunks, conversation messages) is Fernet-encrypted in SQLite. Hybrid
    retrieval = vector top-4k prefilter → decrypt candidates → in-memory
    BM25 → reciprocal-rank fusion. Trade-off: keyword recall is bounded by
    the vector prefilter; at personal scale with 4× oversampling this is
    negligible, and it means no plaintext search index on disk.
12. **Everything said to JARVIS is privacy-tagged `personal`** and therefore
    served by local tiers only, unless `routing.yaml` explicitly sets
    `allow_cloud_for_tagged: true`. The router proved this live: with cloud
    keys present but content tagged, cloud tiers are skipped by policy, not
    by luck (tested in `tests/test_router.py`).
13. **Router consults the node monitor before dialing.** A tier the monitor
    already knows is down is skipped without burning a connect timeout;
    an unknown state is tried optimistically.
14. **Capture is decoupled through the durable bus.** Collectors POST to
    `/capture/event`, which only enqueues; a bus consumer does the
    chunk/embed/store work. A slow embed can't back-pressure collectors,
    and queued events survive hub restarts. Collector cursors advance only
    after successful delivery, so a hub outage means re-emission, not loss.
15. **Redaction happens inside the collector process** (regexes from
    `capture.yaml`), before events cross any network or disk boundary.
    Verified in the live demo: a `password=...` written into a watched note
    is `[REDACTED]` everywhere downstream.
16. **Collectors baseline on first run** (shell history, filesystem,
    browser): they cursor to "now" instead of ingesting years of backlog,
    then tail incrementally.

## Tools & the agent loop (Phase 3)

Every LLM tool call goes through one gate: **kill switch → permission level →
allow-list → audit**. Permission levels (`config/permissions.yaml`,
per-tool overridable): `read`/`act` run immediately; `destructive` parks in
`pending_confirmations` and only runs after the operator replies
`confirm <token>` (intercepted deterministically — never via the model);
`shell` runs only commands matching `shell.allowlist` (no confirmation
bypasses the list). Tool output and retrieved memory are always fenced as
untrusted DATA in the prompt, so a hostile web page or email can't drive a
tool call. Adding a tool = drop a module in `jarvis/tools/` exporting a
`TOOLS` list; it auto-registers (verified: 14 tools discovered).

Model-agnostic tool calling uses a strict one-JSON-object protocol so it
works with local Ollama models that lack native function-calling. The loop
guards against small-model failure modes: it normalizes mangled protocol
output, never shows raw JSON to the operator, detects duplicate tool calls,
and force-synthesizes a plain answer instead of looping or dumping JSON.

**Honest small-model caveat.** On the hub's 3B fallback (`qwen2.5:3b`, chosen
over `llama3.2:3b` for far better JSON/tool adherence) tool *selection* is
good but not perfect: explicit phrasing ("run df -h and tell me…") lands the
right tool more reliably than oblique phrasing ("what's the disk usage?").
The MacBook's larger model (`qwen2.5:14b`) and the cloud tiers handle this
cleanly; the router prefers them when available. The gating, confirmation,
and audit machinery is model-independent and fully covered by tests. One
observed quirk: because every exchange is re-ingested into memory, a small
model can *parrot* a previously retrieved confirmation prompt verbatim
instead of issuing a fresh tool call — the deterministic `confirm <token>`
path is unaffected, but it's a real limitation of tiny local models, noted
rather than hidden.

## Security model (Phase 0 baseline — all implemented)

- **Network**: designed for a Tailscale mesh; hub binds `127.0.0.1` by
  default and should never bind a public interface. Nothing in this repo
  opens a public port. If something must ever be public, put it behind
  Caddy with TLS — not directly.
- **Auth**: every HTTP endpoint requires `x-api-key` (constant-time compare);
  401 otherwise. Boot fails without a key. Telegram (Phase 1) will be locked
  to allow-listed user IDs from `.env`.
- **Rate limiting**: sliding-window per client IP, ordered before auth.
- **Input validation**: pydantic models with length/pattern constraints on
  every request body and query param.
- **Secrets**: `.env` only (0600, gitignored), `.env.example` documents every
  variable; `Secrets.__repr__` is redacted so keys can't leak into logs.
- **Audit**: append-only hash chain, `jarvis audit verify`.
- **Kill switch**: `jarvis pause` / dashboard button (Phase 6) / `POST /control/pause`.
- **Memory encryption**: Fernet vault + HKDF key derivation, key management
  documented above.
- **Prompt-injection posture** (enforced structurally from Phase 1): captured
  and fetched content is *data* — it is never concatenated into system
  prompts and can never trigger tools without operator confirmation.

## Operating the hub

```bash
jarvis status                 # health, nodes, RAM, killswitch
jarvis pause --reason "..."   # kill switch on (persists across restarts)
jarvis resume
jarvis bus tail [topic]       # watch the message log
jarvis bus publish t '{"k":1}'
jarvis audit tail             # what has JARVIS done
jarvis audit verify           # is the trail intact
```

Config lives in `config/jarvis.yaml` (`JARVIS_CONFIG` to override), data in
`~/.jarvis` (`JARVIS_DATA_DIR`; `/var/lib/jarvis` under systemd), secrets in
`.env` (`/etc/jarvis/jarvis.env` under systemd).

## Adding a tool (worked example)

Drop this in `jarvis/tools/weather.py` and restart — it auto-registers:

```python
from jarvis.tools.registry import Tool, ToolContext, ToolError

async def weather_now(args: dict, ctx: ToolContext) -> str:
    city = (args.get("city") or "").strip()
    if not city:
        raise ToolError("city is required")
    # ... call an API via httpx, return a string ...
    return f"Weather for {city}: ..."

TOOLS = [Tool(
    name="weather_now",
    description="Current weather for a city.",
    params={"city": {"type": "string", "required": True}},
    handler=weather_now,
    permission="read",          # read | act | destructive | shell
)]
```

`ToolContext` gives you `cfg`, `store` (memory), `router` (LLM), `bus`,
`audit`, and `permissions`. Mark state-changing tools `act`, dangerous ones
`destructive` (auto-parks for confirmation).

## Adding a collector (Phase 2)

Subclass `jarvis.capture.base.Collector`, implement `async def poll(self) ->
list[CaptureEvent]`, and register it in `jarvis/capture/runner.py:COLLECTORS`
plus a block in `config/capture.yaml`. Redact in the collector before
returning events.
