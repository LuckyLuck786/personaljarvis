"""The hub daemon: FastAPI app exposing health, control (kill switch),
bus introspection, and the audit trail. Runs 24/7 on the Mac Mini under
systemd with a hard memory cap. Everything else (memory, cognition,
interfaces) mounts onto this same process in later phases — one lean
process, not a fleet of them, because RAM is the scarce resource.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import asyncio

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import jarvis
from jarvis.cognition.agent import ChatAgent
from jarvis.cognition.conversation import ConversationLog
from jarvis.cognition.toolloop import ToolLoop
from jarvis.proactive.engine import ProactiveEngine
from jarvis.proactive.followups import FollowupStore
from jarvis.tools.registry import ToolContext, ToolRegistry, load_permissions
from jarvis.core import db
from jarvis.core.audit import AuditLog
from jarvis.core.bus import Bus
from jarvis.core.config import Config, load_config
from jarvis.core.crypto import Vault
from jarvis.core.db import migrate
from jarvis.core.killswitch import KillSwitch
from jarvis.core.logging import get_logger, setup_logging
from jarvis.core.security import (
    ApiKeyMiddleware,
    KillSwitchMiddleware,
    RateLimitMiddleware,
)
from jarvis.hub.health import NodeMonitor, cloud_tier_status, hub_self_health
from jarvis.interfaces.telegram_bot import TelegramInterface
from jarvis.memory.embeddings import OllamaEmbedder
from jarvis.memory.store import MemoryStore
from jarvis.router.router import ModelRouter, load_routing

log = get_logger(__name__)


class PauseRequest(BaseModel):
    reason: str = Field(default="operator request", max_length=500)


class PublishRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9_.\-]+$")
    payload: dict = Field(default_factory=dict)


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    session: str | None = Field(default=None, max_length=100)


class IngestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    kind: str = Field(default="note", pattern=r"^[a-z_]+$", max_length=30)
    source: str = Field(default="api", max_length=100)
    tags: list[str] = Field(default_factory=lambda: ["personal"], max_length=10)


class DispatchRequest(BaseModel):
    agent: str = Field(pattern=r"^(researcher|coder|summarizer)$")
    objective: str = Field(min_length=1, max_length=4000)


class CaptureEventIn(BaseModel):
    source: str = Field(min_length=1, max_length=50, pattern=r"^[a-z_]+$")
    kind: str = Field(min_length=1, max_length=30, pattern=r"^[a-z_]+$")
    content: str = Field(min_length=1, max_length=200_000)
    meta: dict = Field(default_factory=dict)
    ts: float | None = None
    tags: list[str] = Field(default_factory=lambda: ["capture", "personal"],
                            max_length=10)


def _build_embedder(cfg: Config, routing: dict) -> OllamaEmbedder:
    """The embed route's first tier defines where embeddings run — the hub's
    own Ollama by design, so memory never blocks on the laptop."""
    tier_name = routing["routes"]["embed"][0]
    tier = next(t for t in routing["tiers"] if t["name"] == tier_name)
    node = cfg.nodes.get(tier.get("node", ""))
    if not node or not node.ollama_url:
        raise ValueError(
            f"embed tier '{tier_name}' has no node/ollama_url in config/jarvis.yaml"
        )
    return OllamaEmbedder(node.ollama_url, tier["models"]["embed"])


def create_app(cfg: Config | None = None, *, embedder=None, router=None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.logging.level, cfg.logging.pretty or None)

    # Fail fast here — middleware construction is deferred to app startup,
    # and an unauthenticated hub must never get that far.
    if not cfg.secrets.jarvis_api_key:
        raise ValueError(
            "JARVIS_API_KEY is not set — refusing to start an unauthenticated hub."
            " Run `jarvis keygen`."
        )

    if not cfg.secrets.jarvis_master_key:
        raise ValueError(
            "JARVIS_MASTER_KEY is not set — memory encryption is mandatory."
            " Run `jarvis keygen`."
        )

    migrate(cfg.db_path)
    bus = Bus(cfg.db_path, cfg.bus.poll_interval_s, cfg.bus.max_attempts)
    audit = AuditLog(cfg.db_path)
    killswitch = KillSwitch(cfg.db_path)
    monitor = NodeMonitor(cfg, bus)
    started_at = time.time()

    # -- Phase 1 core: memory, router, cognition, telegram --------------------
    vault = Vault(cfg.secrets.jarvis_master_key)
    routing = load_routing()
    if embedder is None:
        embedder = _build_embedder(cfg, routing)
    store = MemoryStore(cfg, vault, embedder)
    if router is None:
        router = ModelRouter(cfg, routing, bus, node_status=monitor.statuses)
    conversations = ConversationLog(cfg.db_path, vault)
    registry = ToolRegistry(ToolContext(
        cfg=cfg, store=store, router=router, bus=bus, audit=audit,
        permissions=load_permissions(),
    ))
    registry.discover()
    toolloop = ToolLoop(registry, router, killswitch)
    followups = FollowupStore(cfg.db_path, vault)

    # Phase 7 advanced
    from jarvis.advanced.improve import SelfImprovement
    from jarvis.advanced.subagents import SubAgentDispatcher
    from jarvis.advanced.twin import DigitalTwin
    twin = DigitalTwin(cfg, store, router, vault, audit)
    improver = SelfImprovement(cfg, router, vault, audit)
    dispatcher = SubAgentDispatcher(cfg, registry, router, killswitch, vault,
                                    store, audit)

    agent = ChatAgent(cfg, store, router, conversations, killswitch, audit,
                      toolloop=toolloop, registry=registry, followups=followups,
                      twin=twin)
    telegram = TelegramInterface(cfg, agent, killswitch, audit, monitor.statuses)
    proactive = ProactiveEngine(cfg, store, router, bus, audit, killswitch,
                                vault, telegram, registry=registry)
    # let the nightly consolidation also refresh the digital twin
    proactive.twin = twin
    proactive.improver = improver

    from jarvis.hub.dashboard import build_dashboard_router
    dashboard_router = build_dashboard_router(cfg, bus, killswitch, store, audit,
                                              monitor, registry)

    async def _reminder_loop(stop: asyncio.Event, interval_s: float = 20):
        """Fires due reminders: bus event + Telegram push. Respects the kill
        switch. (The Phase 4 proactive engine builds on this.)"""
        from jarvis.tools.tasks import pop_due_reminders

        while not stop.is_set():
            try:
                if not killswitch.is_paused():
                    for payload in pop_due_reminders(cfg.db_path):
                        bus.publish("reminder.due", payload)
                        audit.record("system", "reminder.fired", payload)
                        await telegram.send_to_operator(f"⏰ Reminder: {payload['title']}")
            except Exception:
                log.exception("reminder_loop_error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

    async def _ingest_capture_event(msg):
        """Bus consumer: capture.event → memory. Decoupled from the HTTP
        endpoint so a slow embed can never back-pressure collectors, and
        events survive a hub restart (the bus is durable)."""
        p = msg.payload
        await store.ingest(
            p["content"], kind=p.get("kind", "capture"),
            source=p.get("source", "capture"),
            tags=tuple(p.get("tags", ["capture", "personal"])),
            meta=p.get("meta"), ts=p.get("ts"),
        )

    ingest_stop = asyncio.Event()
    reminder_stop = asyncio.Event()
    anomaly_stop = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        monitor_task = asyncio.create_task(monitor.run())
        ingest_task = asyncio.create_task(
            bus.run_consumer("memory_ingestor", ["capture.event"],
                             _ingest_capture_event, stop=ingest_stop)
        )
        reminder_task = asyncio.create_task(_reminder_loop(reminder_stop))
        proactive_task = asyncio.create_task(proactive.run())
        anomaly_task = asyncio.create_task(
            bus.run_consumer("anomaly_watch", ["system.node_status"],
                             proactive.on_node_status, stop=anomaly_stop)
        )
        await telegram.start()  # no-op with truthful log line if unconfigured
        bus.publish("system.hub", {"event": "started", "version": jarvis.__version__})
        log.info("hub_started", version=jarvis.__version__, data_dir=str(cfg.data_dir),
                 telegram="on" if telegram.configured else "off")
        yield
        await telegram.stop()
        monitor.stop()
        proactive.stop()
        ingest_stop.set()
        reminder_stop.set()
        anomaly_stop.set()
        await ingest_task
        await reminder_task
        await proactive_task
        await anomaly_task
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        bus.publish("system.hub", {"event": "stopped"})

    app = FastAPI(title="JARVIS hub", version=jarvis.__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    # Order matters (outermost first at request time): rate limit -> auth -> killswitch
    app.add_middleware(KillSwitchMiddleware, killswitch=killswitch)
    app.add_middleware(ApiKeyMiddleware, api_key=cfg.secrets.jarvis_api_key)
    app.add_middleware(RateLimitMiddleware, limit_per_min=cfg.hub.rate_limit_per_min)

    # expose for tests / composition
    app.state.cfg = cfg
    app.state.bus = bus
    app.state.audit = audit
    app.state.killswitch = killswitch
    app.state.monitor = monitor
    app.state.store = store
    app.state.router = router
    app.state.agent = agent
    app.state.proactive = proactive
    app.state.followups = followups
    app.state.twin = twin
    app.state.improver = improver
    app.state.dispatcher = dispatcher

    # -- health ---------------------------------------------------------

    @app.get("/health")
    def health():
        nodes = monitor.statuses
        configured = [n for n, c in cfg.nodes.items() if c.ollama_url]
        degraded = any(s.get("status") == "down" for s in nodes.values())
        return {
            "status": "degraded" if degraded else "ok",
            "version": jarvis.__version__,
            "killswitch": killswitch.state(),
            "hub": hub_self_health(cfg, bus, started_at),
            "nodes": {n: nodes.get(n, {"status": "unknown"}) for n in configured},
            "cloud": cloud_tier_status(cfg),
        }

    # -- control (kill switch) -------------------------------------------

    @app.post("/control/pause")
    def pause(body: PauseRequest, request: Request):
        killswitch.pause(body.reason)
        audit.record("operator", "control.pause", {"reason": body.reason,
                     "client": request.client.host if request.client else None})
        log.warning("killswitch_engaged", reason=body.reason)
        return killswitch.state()

    @app.post("/control/resume")
    def resume(request: Request):
        killswitch.resume()
        audit.record("operator", "control.resume",
                     {"client": request.client.host if request.client else None})
        log.info("killswitch_released")
        return killswitch.state()

    @app.get("/control/state")
    def control_state():
        return killswitch.state()

    # -- bus ---------------------------------------------------------------

    @app.post("/bus/publish")
    def bus_publish(body: PublishRequest):
        message_id = bus.publish(body.topic, body.payload, actor="api")
        return {"id": message_id, "topic": body.topic}

    @app.get("/bus/tail")
    def bus_tail(
        topic: str | None = Query(default=None, max_length=200),
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        messages = bus.tail(topic=topic, after_id=after_id, limit=limit)
        return {"messages": [m.__dict__ for m in messages]}

    # -- chat + memory (Phase 1) ----------------------------------------------

    @app.post("/chat")
    async def chat(body: ChatRequest):
        return await agent.handle(body.text, interface="api", external_id=body.session)

    @app.post("/memory/ingest")
    async def memory_ingest(body: IngestRequest):
        doc_id = await store.ingest(body.text, kind=body.kind, source=body.source,
                                    tags=tuple(body.tags))
        audit.record("api", "memory.ingest", {"doc_id": doc_id, "kind": body.kind})
        return {"doc_id": doc_id}

    @app.get("/memory/search")
    async def memory_search(
        q: str = Query(min_length=1, max_length=1000),
        k: int = Query(default=6, ge=1, le=50),
    ):
        results = await store.search(q, k=k)
        return {"results": [r.__dict__ for r in results]}

    @app.get("/memory/recent")
    def memory_recent(limit: int = Query(default=20, ge=1, le=200)):
        return {"docs": store.recent_docs(limit=limit)}

    # -- capture (Phase 2) ------------------------------------------------------

    @app.post("/capture/event")
    def capture_event(body: CaptureEventIn):
        message_id = bus.publish(
            "capture.event",
            {"source": body.source, "kind": body.kind, "content": body.content,
             "meta": body.meta, "ts": body.ts or time.time(), "tags": body.tags},
            actor=f"collector:{body.source}",
        )
        return {"queued": message_id}

    @app.get("/timeline")
    def timeline(
        hours: float = Query(default=24, gt=0, le=24 * 90),
        kind: str | None = Query(default=None, max_length=30),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        since = time.time() - hours * 3600
        return {"events": store.timeline(since, kinds=[kind] if kind else None,
                                         limit=limit)}

    # -- proactive (Phase 4) ----------------------------------------------------

    @app.post("/proactive/run/{job_name}")
    async def proactive_run(job_name: str):
        """Trigger a job on demand (for demos / testing). Returns its output."""
        conn = db.connect(cfg.db_path)
        try:
            row = conn.execute("SELECT id, name, kind, schedule, meta FROM"
                               " scheduled_jobs WHERE name=?", (job_name,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return JSONResponse({"detail": f"no job {job_name}"}, status_code=404)
        handler = proactive.handlers.get(row["kind"])
        if handler is None:
            return JSONResponse({"detail": "no handler"}, status_code=400)
        result = await handler(dict(row))
        return {"job": job_name, "result": result}

    @app.get("/proactive/jobs")
    def proactive_jobs():
        conn = db.connect(cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT name, kind, schedule, enabled, next_run, last_run,"
                " last_status FROM scheduled_jobs ORDER BY next_run"
            ).fetchall()
        finally:
            conn.close()
        return {"jobs": [dict(r) for r in rows]}

    @app.get("/followups")
    def followups_list():
        return {"followups": followups.open_followups()}

    @app.post("/followups/{followup_id}/resolve")
    def followups_resolve(followup_id: int):
        ok = followups.resolve(followup_id, "done")
        return {"resolved": ok}

    # -- advanced (Phase 7) -----------------------------------------------------

    @app.post("/advanced/improve")
    async def advanced_improve():
        proposals = await improver.propose()
        return {"proposals": proposals}

    @app.get("/advanced/proposals")
    def advanced_proposals(status: str = Query(default="proposed", max_length=20)):
        return {"proposals": improver.list_proposals(status)}

    @app.post("/advanced/proposals/{proposal_id}/{decision}")
    def advanced_proposal_decide(proposal_id: int, decision: str):
        status = {"accept": "accepted", "dismiss": "dismissed"}.get(decision)
        if status is None:
            return JSONResponse({"detail": "decision must be accept|dismiss"},
                                status_code=400)
        return {"updated": improver.set_status(proposal_id, status)}

    @app.post("/advanced/dispatch")
    async def advanced_dispatch(body: DispatchRequest):
        run_id = dispatcher.queue(body.agent, body.objective)
        # run inline for the API (also queryable async via /advanced/run)
        result = await dispatcher.run(run_id)
        return result

    @app.get("/advanced/run/{run_id}")
    def advanced_run(run_id: int):
        run = dispatcher.get_run(run_id)
        if run is None:
            return JSONResponse({"detail": "no such run"}, status_code=404)
        return run

    @app.get("/advanced/twin")
    def advanced_twin():
        return twin.current() or {"summary": None, "note": "not built yet"}

    @app.post("/advanced/twin/rebuild")
    async def advanced_twin_rebuild():
        return await twin.rebuild()

    # -- audit ---------------------------------------------------------------

    @app.get("/audit/tail")
    def audit_tail(limit: int = Query(default=50, ge=1, le=500)):
        return {"entries": audit.tail(limit)}

    @app.get("/audit/verify")
    def audit_verify():
        return audit.verify()

    app.include_router(dashboard_router)
    return app


def serve() -> None:
    import uvicorn

    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.hub.bind_host, port=cfg.hub.port, log_level="warning")
