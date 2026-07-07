"""Tasks & reminders. Reminders created here are fired by the hub's
reminder loop (bus topic `reminder.due` → Telegram push when configured)."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from jarvis.core import db
from jarvis.tools.registry import Tool, ToolContext, ToolError

_REL_RE = re.compile(r"^\+(\d+)([mhd])$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_due(spec: str | None) -> float | None:
    """'18:00' (today, or tomorrow if past) | 'tomorrow 09:00' |
    '2026-07-09 14:00' | '+30m' / '+2h' / '+1d' → unix ts."""
    if not spec:
        return None
    spec = spec.strip().lower()
    now = datetime.now()
    if m := _REL_RE.match(spec):
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n),
                 "d": timedelta(days=n)}[unit]
        return (now + delta).timestamp()
    if m := _HHMM_RE.match(spec):
        due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                          second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        return due.timestamp()
    if spec.startswith("tomorrow"):
        rest = spec.removeprefix("tomorrow").strip() or "09:00"
        if m := _HHMM_RE.match(rest):
            due = (now + timedelta(days=1)).replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return due.timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(spec, fmt).timestamp()
        except ValueError:
            continue
    raise ToolError(
        f"can't parse due time {spec!r} — use HH:MM, 'tomorrow HH:MM', "
        "'YYYY-MM-DD HH:MM', or '+30m/+2h/+1d'"
    )


def _fmt_ts(ts: float | None) -> str:
    return time.strftime("%a %Y-%m-%d %H:%M", time.localtime(ts)) if ts else "no deadline"


def pop_due_reminders(db_path) -> list[dict]:
    """Atomically claim reminders that are due: returns them and marks them
    reminded so a crash between fire and push can't double-remind."""
    conn = db.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, title, due_ts FROM tasks WHERE status='open'"
                " AND remind=1 AND reminded_at IS NULL AND due_ts<=?",
                (time.time(),),
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE tasks SET reminded_at=? WHERE id=?",
                             (time.time(), r["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return [{"task_id": r["id"], "title": r["title"], "due_ts": r["due_ts"]}
            for r in rows]


async def task_add(args: dict, ctx: ToolContext) -> str:
    title = (args.get("title") or "").strip()
    if not title:
        raise ToolError("title is required")
    due_ts = parse_due(args.get("due"))
    remind = 1 if (args.get("remind", True) and due_ts) else 0
    conn = db.connect(ctx.cfg.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO tasks (title, body, due_ts, remind, created_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (title, args.get("body", ""), due_ts, remind, time.time()),
        )
        task_id = cur.lastrowid
    finally:
        conn.close()
    ctx.bus.publish("task.created", {"id": task_id, "title": title, "due_ts": due_ts})
    suffix = f", reminder at {_fmt_ts(due_ts)}" if remind else (
        f", due {_fmt_ts(due_ts)}" if due_ts else "")
    return f"Task #{task_id} added: {title}{suffix}"


async def task_list(args: dict, ctx: ToolContext) -> str:
    status = args.get("status", "open")
    conn = db.connect(ctx.cfg.db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, due_ts, status FROM tasks WHERE status=?"
            " ORDER BY due_ts IS NULL, due_ts LIMIT 30",
            (status,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"No {status} tasks."
    return "\n".join(f"#{r['id']} {r['title']} — {_fmt_ts(r['due_ts'])}" for r in rows)


async def task_complete(args: dict, ctx: ToolContext) -> str:
    task_id = args.get("id")
    if not isinstance(task_id, int):
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            raise ToolError("id (integer) is required")
    conn = db.connect(ctx.cfg.db_path)
    try:
        cur = conn.execute(
            "UPDATE tasks SET status='done', completed_ts=? WHERE id=? AND status='open'",
            (time.time(), task_id),
        )
        if cur.rowcount == 0:
            raise ToolError(f"no open task #{task_id}")
        title = conn.execute("SELECT title FROM tasks WHERE id=?",
                             (task_id,)).fetchone()["title"]
    finally:
        conn.close()
    return f"Task #{task_id} done: {title}"


TOOLS = [
    Tool(
        name="task_add",
        description=("Create a task/reminder. due accepts HH:MM, 'tomorrow HH:MM', "
                     "'YYYY-MM-DD HH:MM', or +30m/+2h/+1d. A due time gets a "
                     "reminder push by default (remind=false to disable)."),
        params={"title": {"type": "string", "required": True},
                "due": {"type": "string"},
                "body": {"type": "string"},
                "remind": {"type": "boolean"}},
        handler=task_add,
        permission="act",
    ),
    Tool(
        name="task_list",
        description="List tasks. status: open (default) | done | cancelled.",
        params={"status": {"type": "string"}},
        handler=task_list,
        permission="read",
    ),
    Tool(
        name="task_complete",
        description="Mark task #id as done.",
        params={"id": {"type": "integer", "required": True}},
        handler=task_complete,
        permission="act",
    ),
]
