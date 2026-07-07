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
from pydantic import BaseModel, Field

import jarvis
from jarvis.cognition.agent import ChatAgent
from jarvis.cognition.conversation import ConversationLog
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
    agent = ChatAgent(cfg, store, router, conversations, killswitch, audit)
    telegram = TelegramInterface(cfg, agent, killswitch, audit, monitor.statuses)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        monitor_task = asyncio.create_task(monitor.run())
        await telegram.start()  # no-op with truthful log line if unconfigured
        bus.publish("system.hub", {"event": "started", "version": jarvis.__version__})
        log.info("hub_started", version=jarvis.__version__, data_dir=str(cfg.data_dir),
                 telegram="on" if telegram.configured else "off")
        yield
        await telegram.stop()
        monitor.stop()
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

    # -- audit ---------------------------------------------------------------

    @app.get("/audit/tail")
    def audit_tail(limit: int = Query(default=50, ge=1, le=500)):
        return {"entries": audit.tail(limit)}

    @app.get("/audit/verify")
    def audit_verify():
        return audit.verify()

    return app


def serve() -> None:
    import uvicorn

    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.hub.bind_host, port=cfg.hub.port, log_level="warning")
