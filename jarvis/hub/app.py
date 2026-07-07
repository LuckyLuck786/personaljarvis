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
from jarvis.core.audit import AuditLog
from jarvis.core.bus import Bus
from jarvis.core.config import Config, load_config
from jarvis.core.db import migrate
from jarvis.core.killswitch import KillSwitch
from jarvis.core.logging import get_logger, setup_logging
from jarvis.core.security import (
    ApiKeyMiddleware,
    KillSwitchMiddleware,
    RateLimitMiddleware,
)
from jarvis.hub.health import NodeMonitor, cloud_tier_status, hub_self_health

log = get_logger(__name__)


class PauseRequest(BaseModel):
    reason: str = Field(default="operator request", max_length=500)


class PublishRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9_.\-]+$")
    payload: dict = Field(default_factory=dict)


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    setup_logging(cfg.logging.level, cfg.logging.pretty or None)

    # Fail fast here — middleware construction is deferred to app startup,
    # and an unauthenticated hub must never get that far.
    if not cfg.secrets.jarvis_api_key:
        raise ValueError(
            "JARVIS_API_KEY is not set — refusing to start an unauthenticated hub."
            " Run `jarvis keygen`."
        )

    migrate(cfg.db_path)
    bus = Bus(cfg.db_path, cfg.bus.poll_interval_s, cfg.bus.max_attempts)
    audit = AuditLog(cfg.db_path)
    killswitch = KillSwitch(cfg.db_path)
    monitor = NodeMonitor(cfg, bus)
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        monitor_task = asyncio.create_task(monitor.run())
        bus.publish("system.hub", {"event": "started", "version": jarvis.__version__})
        log.info("hub_started", version=jarvis.__version__, data_dir=str(cfg.data_dir))
        yield
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
