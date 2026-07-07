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
| 1 — Memory + Chat MVP | RAG (LanceDB + SQLite), hub embeddings, cognition core, Telegram, model router w/ failover | ⬜ not started |
| 2 — Capture pipeline | notes/clipboard/fs/shell/browser collectors, redaction, timeline | ⬜ not started |
| 3 — Tools & actions | plugin system, tasks/reminders/calendar/email/web/shell/home-lab | ⬜ not started |
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
| jarvis-hub daemon (Phase 0) | **~62 MB RSS measured** at boot on dev machine |
| systemd cap | `MemoryHigh=512M`, `MemoryMax=1G` — kernel-enforced ceiling |
| SQLite page cache | capped at 8 MB (`PRAGMA cache_size=-8000`) |
| Ollama on hub (Phase 1) | load-on-demand only; 1–3B quantized models; never pinned |
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

## Adding a tool / collector (Phase 3 / 2)

The plugin contract is fixed now (see `config/permissions.yaml`,
`config/capture.yaml`): a tool is one module in `jarvis/tools/` declaring
`schema`, `handler`, `permission`, `destructive`; collectors are modules in
`jarvis/capture/` configured by `capture.yaml`. Auto-registration lands with
Phase 3/2 respectively — docs will be updated with a worked example then.
