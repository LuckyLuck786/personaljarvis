"""Web dashboard routes.

Deliberately dependency-free: one self-contained HTML page (vanilla JS, no
build step, no framework, no CDN) served by FastAPI. Zero extra RAM, zero
supply-chain surface. The page authenticates with the API key the operator
pastes once (kept in sessionStorage, sent as x-api-key on every fetch) — the
exact same auth path as every other client, so there's no unauthenticated
dashboard endpoint.

The HTML/JS lives in dashboard_page.py to keep this router readable.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from jarvis.core import db
from jarvis.hub.dashboard_page import DASHBOARD_HTML, LOGIN_HTML


def build_dashboard_router(cfg, bus, killswitch, store, audit, monitor,
                           registry) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        # The shell loads unauthenticated (it's just static HTML/JS); every
        # data call it makes is authenticated. The key never touches the URL.
        return HTMLResponse(LOGIN_HTML)

    @router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    # -- data endpoints (all behind the global API-key middleware) -------------

    @router.get("/dashboard/api/overview")
    def overview():
        # routing view: count llm.request by tier over recent history
        rows = bus.tail("llm.request", limit=200)
        tier_counts: dict[str, int] = {}
        for m in rows:
            t = m.payload.get("tier") or "degraded"
            tier_counts[t] = tier_counts.get(t, 0) + 1

        conn = db.connect(cfg.db_path)
        try:
            open_tasks = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
            doc_count = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_docs").fetchone()["n"]
        finally:
            conn.close()

        return {
            "killswitch": killswitch.state(),
            "nodes": monitor.statuses,
            "tier_usage": tier_counts,
            "open_tasks": open_tasks,
            "memory_docs": doc_count,
            "tools": sorted(registry.tools) if registry else [],
            "bus_backlog": bus.depth(),
        }

    @router.get("/dashboard/api/timeline")
    def timeline(hours: float = Query(default=24, gt=0, le=24 * 90),
                 limit: int = Query(default=80, ge=1, le=500)):
        since = time.time() - hours * 3600
        return {"events": store.timeline(since, limit=limit, preview_chars=200)}

    @router.get("/dashboard/api/search")
    async def search(q: str = Query(min_length=1, max_length=500),
                     k: int = Query(default=8, ge=1, le=30)):
        results = await store.search(q, k=k)
        return {"results": [r.__dict__ for r in results]}

    @router.get("/dashboard/api/tasks")
    def tasks():
        conn = db.connect(cfg.db_path)
        try:
            rows = conn.execute(
                "SELECT id, title, due_ts, status FROM tasks"
                " ORDER BY status, due_ts IS NULL, due_ts LIMIT 100"
            ).fetchall()
        finally:
            conn.close()
        return {"tasks": [dict(r) for r in rows]}

    @router.get("/dashboard/api/logs")
    def logs(limit: int = Query(default=60, ge=1, le=300)):
        return {"audit": audit.tail(limit),
                "bus": [m.__dict__ for m in bus.tail(limit=limit)]}

    return router
