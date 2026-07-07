"""Tool plugin system.

A tool is a module in jarvis/tools/ that defines a module-level `TOOLS`
list of Tool objects. The registry discovers them automatically at startup
(pkgutil walk — drop a file in, it registers). Every execution goes through
exactly one gate: kill switch → permission level → allow-list → audit.

Permission levels (config/permissions.yaml can override per-tool):
    read        runs immediately
    act         runs immediately (non-destructive state changes)
    destructive parked in pending_confirmations until the operator replies
                'confirm <token>' — no auto-execution path exists
    shell       only commands matching the allow-list run AT ALL; the
                allow-list has no confirmation bypass
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import yaml

from jarvis.core.logging import get_logger

log = get_logger(__name__)

CONFIRM_TTL_S = 600  # a parked destructive action dies after 10 minutes


@dataclass
class ToolContext:
    """What a tool handler gets to touch. Built once by the hub."""

    cfg: Any                  # jarvis.core.config.Config
    store: Any                # MemoryStore
    router: Any               # ModelRouter
    bus: Any                  # Bus
    audit: Any                # AuditLog
    permissions: dict         # parsed permissions.yaml


@dataclass
class Tool:
    name: str
    description: str          # shown to the model — include arg docs here
    params: dict              # JSON-schema-ish {name: {type, description, required}}
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    permission: str = "read"  # read | act | destructive | shell
    destructive: bool = False
    examples: list[str] = field(default_factory=list)


class ToolError(Exception):
    pass


class ConfirmationRequired(Exception):
    def __init__(self, token: str, tool: str, tool_args: dict):
        super().__init__(f"confirmation required: {token}")
        # NOTE: set AFTER super().__init__ — Exception.__init__ overwrites
        # self.args, so the parked args live on a distinct attribute.
        self.token = token
        self.tool = tool
        self.tool_args = tool_args


def load_permissions(path: str | None = None) -> dict:
    import os
    from pathlib import Path

    p = Path(path or os.environ.get("JARVIS_PERMISSIONS", "config/permissions.yaml"))
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


class ToolRegistry:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self.tools[tool.name] = tool

    def discover(self) -> None:
        """Import every module in jarvis.tools and collect their TOOLS lists."""
        import jarvis.tools as pkg

        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name in ("registry",):
                continue
            module = importlib.import_module(f"jarvis.tools.{info.name}")
            for tool in getattr(module, "TOOLS", []):
                self.register(tool)
        log.info("tools_discovered", tools=sorted(self.tools))

    # -- gate + execute ---------------------------------------------------------

    def _effective_permission(self, tool: Tool) -> str:
        overrides = self.ctx.permissions.get("overrides") or {}
        return overrides.get(tool.name, tool.permission)

    def describe_for_prompt(self) -> str:
        lines = []
        for t in sorted(self.tools.values(), key=lambda t: t.name):
            perm = self._effective_permission(t)
            params = ", ".join(
                f"{k}: {v.get('type', 'string')}{'' if v.get('required') else '?'}"
                for k, v in t.params.items()
            )
            note = " [REQUIRES OPERATOR CONFIRMATION]" if perm == "destructive" else ""
            lines.append(f"- {t.name}({params}): {t.description}{note}")
        return "\n".join(lines)

    async def execute(self, name: str, args: dict, interface: str,
                      killswitch, confirmed: bool = False) -> str:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        if killswitch.is_paused():
            self.ctx.audit.record(interface, f"tool.{name}", args, "denied:killswitch")
            raise ToolError("kill switch engaged — no tools will run")

        permission = self._effective_permission(tool)
        if permission == "destructive" and not confirmed:
            token = self._park(tool, args, interface)
            self.ctx.audit.record(interface, f"tool.{name}", args, f"parked:{token}")
            raise ConfirmationRequired(token, name, args)

        try:
            result = await tool.handler(args, self.ctx)
        except ToolError:
            self.ctx.audit.record(interface, f"tool.{name}", args, "error:toolerror")
            raise
        except Exception as exc:
            log.exception("tool_failed", tool=name)
            self.ctx.audit.record(interface, f"tool.{name}", args,
                                  f"error:{type(exc).__name__}")
            raise ToolError(f"{name} failed: {type(exc).__name__}") from exc
        self.ctx.audit.record(interface, f"tool.{name}",
                              {"args": args, "confirmed": confirmed}, "ok")
        return result

    # -- confirmation flow --------------------------------------------------------

    def _park(self, tool: Tool, args: dict, interface: str) -> str:
        from jarvis.core import db

        token = secrets.token_hex(3)  # short enough to type from a phone
        conn = db.connect(self.ctx.cfg.db_path)
        try:
            conn.execute(
                "INSERT INTO pending_confirmations"
                " (token, tool, args, interface, created_ts, expires_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (token, tool.name, json.dumps(args), interface,
                 time.time(), time.time() + CONFIRM_TTL_S),
            )
        finally:
            conn.close()
        return token

    async def confirm(self, token: str, interface: str, killswitch) -> str:
        from jarvis.core import db

        conn = db.connect(self.ctx.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT tool, args, expires_ts, status FROM pending_confirmations"
                " WHERE token=?", (token,),
            ).fetchone()
            if row is None:
                raise ToolError(f"no pending action with token {token}")
            if row["status"] != "pending":
                raise ToolError(f"action {token} already {row['status']}")
            if time.time() > row["expires_ts"]:
                conn.execute("UPDATE pending_confirmations SET status='expired'"
                             " WHERE token=?", (token,))
                raise ToolError(f"action {token} expired — ask again")
            conn.execute("UPDATE pending_confirmations SET status='executed'"
                         " WHERE token=?", (token,))
        finally:
            conn.close()
        self.ctx.audit.record(interface, "tool.confirm", {"token": token}, "ok")
        return await self.execute(row["tool"], json.loads(row["args"]),
                                  interface, killswitch, confirmed=True)
