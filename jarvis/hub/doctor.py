"""Pre-flight diagnostics: `jarvis doctor`.

Runs LOCALLY (no need for the hub to be up) so it's usable before/while
deploying. Checks the whole chain — config, secrets, DB/migrations, crypto,
Ollama reachability + required models, disk headroom, audit integrity — and
prints a pass/warn/fail report with actionable fixes. Exit code is non-zero
if anything is a hard FAIL, so it doubles as a deploy gate.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

import httpx

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def _check_secrets(cfg) -> list[Check]:
    s = cfg.secrets
    out = [
        Check("API key set", OK if s.jarvis_api_key else FAIL,
              fix="run `jarvis keygen`"),
    ]
    # master key must be present AND valid (32-byte urlsafe b64)
    if not s.jarvis_master_key:
        out.append(Check("Master key set", FAIL, fix="run `jarvis keygen`"))
    else:
        try:
            from jarvis.core.crypto import Vault
            Vault(s.jarvis_master_key).encrypt("doctor", "ping")
            out.append(Check("Master key valid (encryption works)", OK))
        except Exception as exc:
            out.append(Check("Master key valid", FAIL, detail=str(exc),
                             fix="regenerate with `jarvis keygen --force` "
                                 "(WARNING: invalidates existing encrypted memory)"))
    tg = bool(s.telegram_bot_token and s.telegram_allowed_user_ids)
    out.append(Check("Telegram configured", OK if tg else WARN,
                     detail="" if tg else "bot won't run",
                     fix="" if tg else "set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS"))
    cloud = any([s.groq_api_key, s.cerebras_api_key, s.gemini_api_key])
    out.append(Check("Cloud fallback configured", OK if cloud else WARN,
                     detail="" if cloud else "optional — the local hub model is primary",
                     fix="" if cloud else "add GROQ_API_KEY for a fast cloud fallback (optional)"))
    return out


def _check_db(cfg) -> list[Check]:
    from jarvis.core import db
    from jarvis.core.db import migrate

    out = []
    try:
        applied = migrate(cfg.db_path)  # idempotent; applies anything pending
        conn = db.connect(cfg.db_path)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"]
        finally:
            conn.close()
        out.append(Check("Database + migrations", OK,
                         detail=f"{n} migrations applied"
                                + (f" (+{len(applied)} just now)" if applied else "")))
    except Exception as exc:
        out.append(Check("Database + migrations", FAIL, detail=str(exc)))
        return out
    # audit chain
    try:
        from jarvis.core.audit import AuditLog
        v = AuditLog(cfg.db_path).verify()
        out.append(Check("Audit chain integrity",
                         OK if v["ok"] else FAIL,
                         detail=f"{v['entries']} entries"
                                + ("" if v["ok"] else f", broken at {v['first_bad_id']}")))
    except Exception as exc:
        out.append(Check("Audit chain integrity", WARN, detail=str(exc)))
    return out


def _required_models_by_node(cfg) -> dict[str, set[str]]:
    """Which Ollama models each node must have, per config/routing.yaml.
    Missing a hub chat model is exactly what causes 'degraded mode' when the
    MacBook sleeps, so we check it explicitly."""
    try:
        from jarvis.router.router import load_routing
        routing = load_routing()
    except Exception:
        return {}
    wanted: dict[str, set[str]] = {}
    for tier in routing.get("tiers", []):
        if tier.get("kind") != "ollama":
            continue
        node = tier.get("node")
        if not node:
            continue
        wanted.setdefault(node, set()).update(
            m for m in tier.get("models", {}).values() if m
        )
    return wanted


def _model_present(models: list[str], want: str) -> bool:
    # ollama reports "qwen2.5:3b" or "qwen2.5:3b:latest"; match either way
    return any(m == want or m.startswith(want + ":") or m.startswith(want)
               for m in models)


def _check_ollama(cfg) -> list[Check]:
    out = []
    routing_embed_ok = False
    required = _required_models_by_node(cfg)
    for name, node in cfg.nodes.items():
        if not node.ollama_url:
            continue
        try:
            r = httpx.get(f"{node.ollama_url.rstrip('/')}/api/tags", timeout=3)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
            out.append(Check(f"Ollama '{name}' reachable", OK,
                             detail=f"{len(models)} models"))
            # verify every routed model for this node is actually pulled
            for want in sorted(required.get(name, ())):
                present = _model_present(models, want)
                is_hub = name == "hub_ollama" or node.role == "embeddings_and_fallback"
                # a missing model on the hub is FAIL (breaks the always-on
                # path); on the MacBook it's WARN (laptop, cloud/hub cover it)
                sev = OK if present else (FAIL if is_hub else WARN)
                out.append(Check(f"Model '{want}' on '{name}'", sev,
                                 fix="" if present else f"run `ollama pull {want}` on {name}"))
                if "embed" in want and present and is_hub:
                    routing_embed_ok = True
        except Exception:
            sev = WARN if name == "macbook" else FAIL  # macbook may be asleep
            out.append(Check(f"Ollama '{name}' reachable", sev,
                             detail="unreachable",
                             fix=f"start Ollama on {name} ({node.ollama_url})"))
    if not any(n.ollama_url for n in cfg.nodes.values()):
        out.append(Check("Ollama nodes configured", FAIL,
                         fix="set nodes.*.ollama_url in config/jarvis.yaml"))
    elif not routing_embed_ok:
        out.append(Check("Embeddings available", WARN,
                         detail="hub embedding model not confirmed — memory ingest may fail"))
    return out


def _check_disk(cfg) -> list[Check]:
    usage = shutil.disk_usage(cfg.data_dir)
    free_gb = usage.free / (1024 ** 3)
    status = OK if free_gb > 5 else (WARN if free_gb > 1 else FAIL)
    return [Check("Disk headroom", status, detail=f"{free_gb:.1f} GB free")]


def run_doctor(cfg) -> tuple[list[Check], bool]:
    checks: list[Check] = []
    checks += _check_secrets(cfg)
    checks += _check_db(cfg)
    checks += _check_ollama(cfg)
    checks += _check_disk(cfg)
    healthy = not any(c.status == FAIL for c in checks)
    return checks, healthy


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        line = f"  [{_MARK[c.status]}] {c.name}"
        if c.detail:
            line += f" — {c.detail}"
        lines.append(line)
        if c.status != OK and c.fix:
            lines.append(f"        → {c.fix}")
    return "\n".join(lines)
