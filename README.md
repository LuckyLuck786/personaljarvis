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
| **4 — Proactive engine** | restart-safe scheduler, morning/evening digests → Telegram, reminder firing, commitment follow-ups ("you said you'd…"), node-down anomaly alerts, nightly memory consolidation | ✅ **done, running** — degrades to a plain digest if every LLM tier is down, so it still messages you while the MacBook sleeps |
| **5 — Voice** | wake word ("Jarvis", openWakeWord) → STT (faster-whisper) → hub `/chat` → TTS (Piper), honest capability detection + push-to-talk/text fallbacks | ✅ **done, running** — audio backends are optional extras (`.[voice]` + Piper) that live on the MacBook; verified end-to-end in text-fallback mode. The wake-word→speak path needs a mic + the extras installed |
| **6 — Web dashboard** | single self-contained page (no framework/CDN/build): overview metrics, node health, model-routing bars, tool list, memory search, task board, 24h timeline, live audit/bus feed, kill-switch toggle | ✅ **done, running** — verified rendering + interactive (search, pause/resume) in a browser; auth-gated, stays reachable when paused so you can always resume |
| **7 — Advanced** | self-improvement loop (evidence-based pattern mining → automation proposals, operator-approved only), multi-agent sub-task dispatch (tool-scoped researcher/coder/summarizer), self-maintaining digital twin injected into chat context | ✅ **done, running** — LoRA fine-tuning is intentionally left as a documented, non-fabricated next step (see below) |

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

**Default profile is Mini-only.** Out of the box the config routes all
inference to the hub's local Ollama (`hub_ollama` first in every route), so
the Mac Mini is a fully self-contained assistant with no dependency on the
MacBook. The MacBook and cloud tiers are optional fallbacks you enable by
adding the node / an API key. The Telegram bot long-polls outbound, so it
works from anywhere with **no open ports and no public IP** — see DEPLOY.md.

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

## Getting it running

- **[DEPLOY.md](DEPLOY.md)** — full step-by-step for your two-node setup
  (Tailscale → Ollama → hub install → Telegram → capture/voice/dashboard).
- **[QUICKSTART.md](QUICKSTART.md)** — fastest path on a single dev machine.

Short version:

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
| jarvis-hub, full system wired (all 8 phases, RAG loaded) | **~135 MB RSS measured** — 13% of the 6 GB hub |
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
11. **Vectors live in SQLite as float32 blobs; search is pure-Python cosine
    — no LanceDB/pyarrow.** Those native libraries require AVX and *SIGILL on
    pre-2011 CPUs* (this bit a real 2010 Mac Mini deployment), and they're
    heavy on a 6 GB box. Normalized embeddings are stored per chunk; retrieval
    is brute-force cosine (== dot product) → BM25 over the top candidates →
    reciprocal-rank fusion. O(N·dim) per query is trivial at personal scale
    (thousands of chunks) and runs on ANY CPU. Content stays Fernet-encrypted;
    only numbers sit in the vector table. `jarvis reindex` re-embeds chunks
    (e.g. after migrating off the old LanceDB store).
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

## Knowledge graph (who / what / when)

Ingesting memory also populates a lightweight entity graph (the
`entities`/`relations` tables). Extraction is deterministic and cheap — no
spaCy/transformers, which would blow the 6 GB budget — favouring precision:
multi-word proper names, `project X` patterns, and `@handles`. Entities that
appear in the same note/chat get a weighted `co_occurs` edge, so "who is
connected to X" and "who do I deal with most" fall out of the accumulated
weights. Only entity *names* live in these tables; the surrounding content
stays Fernet-encrypted in `memory_docs`.

```bash
jarvis graph --backfill      # populate from existing memory (one-time)
jarvis graph "Sarah Chen"    # → Aurora (project, strength 4), Marcus Lee, David Kim
jarvis graph                 # most-connected entities overall
```

The agent can query it too, via the `graph_connections` / `graph_top` tools.
Typed person-vs-org refinement is an optional LLM pass during nightly
consolidation; the base graph never depends on a model being reachable.

## Operations: backup, restore, doctor

- **`jarvis backup [--out file]`** snapshots the SQLite DB (via SQLite's
  online-backup API — consistent even while the hub runs) + the LanceDB
  vector store into one checksummed `.tar.gz`. Memory content is already
  encrypted inside those files, so the archive inherits that encryption. The
  archive does **not** contain `JARVIS_MASTER_KEY` — back that up once,
  separately.
- **`jarvis restore <archive> [--force]`** verifies checksums first, moves
  any existing data aside as `*.pre-restore-*` (never deletes), then restores.
- **`jarvis doctor`** runs pre-flight diagnostics *without needing the hub
  up*: secrets present + master key actually decrypts, migrations applied,
  audit chain intact, Ollama reachable with the embedding model present, disk
  headroom. Exits non-zero on any hard FAIL, so it doubles as a deploy gate.

## Proactive engine (Phase 4)

A single scheduler loop claims due jobs atomically (restart-safe, no
double-fire) and dispatches them; everything respects the kill switch:

- **Digests** (`daily@07:30` / `20:30`): calendar + open tasks + outstanding
  commitments, summarized by the router and pushed to Telegram. If every
  tier is down it sends the structured material verbatim — a degraded but
  honest digest — so you're never left in silence while the laptop sleeps.
- **Follow-ups**: commitments are detected deterministically from your own
  words ("I'll email Sam tomorrow" → recorded, no LLM, no hallucinated
  commitments) and nudged when their soft deadline passes.
- **Anomaly alerts**: event-driven off `system.node_status` — a node going
  down pings you promptly, not on a timer.
- **Nightly consolidation** (`daily@03:00`): summarizes the day's
  captures/chats into a durable `daily_summary` and re-ingests it as
  long-term memory — the "second brain" gaining a coherent long-term layer.

Trigger any job on demand: `jarvis digest morning`, or `POST
/proactive/run/<job>`. Inspect with `jarvis jobs` and `jarvis followups`.

## Advanced capabilities (Phase 7)

- **Self-improvement loop**: `jarvis/advanced/patterns.py` mines *real,
  countable* signals from your activity (repeated task titles, frequent tool
  use, recurring browsing) with no LLM in the mining step — so a proposal can
  never be invented for a pattern that isn't in the data. The model only
  *phrases* proposals from those hard signals. Proposals are stored for
  review; **JARVIS never self-executes them** — accepting one is a manual,
  audited action. `jarvis proposals [--generate]`.
- **Multi-agent dispatch**: hand a self-contained objective to a tool-scoped
  sub-agent (researcher / coder / summarizer). Each gets a restricted tool
  allow-list via a scoped registry view, so a researcher structurally cannot
  reach shell or destructive tools. Researcher output is filed back into
  memory. `jarvis dispatch researcher "…"`.
- **Digital twin**: a self-maintaining, versioned personal-context summary
  rebuilt nightly (after consolidation) from your summaries/notes/chats, and
  injected into the chat system prompt so JARVIS always has a compact model
  of who you are and what you're working on. `jarvis twin [--rebuild]`.
- **LoRA personal fine-tuning** (honest status): **not implemented.** The
  spec lists it as optional and it belongs on the MacBook/cloud, never the
  6 GB hub. The captured data and daily summaries are the training corpus
  when you want it; wiring an actual training run is the documented next
  step, deliberately not faked here.

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
