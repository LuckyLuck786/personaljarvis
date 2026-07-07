"""Pattern mining for the self-improvement loop.

Deterministic signal extraction over captured activity + audit — no LLM in
the mining step, so the *evidence* is real and countable. The LLM only comes
in later to phrase a proposal from these hard signals. This ordering is the
honesty guarantee: JARVIS can't propose an automation for a pattern that
isn't actually in the data.

Signals detected:
  * repeated shell commands  → candidate for a saved action / alias tool
  * recurring task titles     → candidate for a scheduled/recurring task
  * frequent web domains      → candidate for a digest/watch
  * frequent tool usage       → candidate for a shortcut
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

from jarvis.core import db

_WORD = re.compile(r"[a-z0-9]+")


def _normalize_title(title: str) -> str:
    return " ".join(_WORD.findall(title.lower()))[:80]


def mine_signals(db_path: str | Path, window_days: int = 30,
                 min_count: int = 3) -> list[dict]:
    """Return concrete, evidenced signals worth proposing an automation for."""
    since = time.time() - window_days * 86400
    conn = db.connect(db_path)
    signals: list[dict] = []
    try:
        # 1. recurring task titles → recurring-task automation
        task_titles = Counter(
            _normalize_title(r["title"])
            for r in conn.execute(
                "SELECT title FROM tasks WHERE created_ts>=?", (since,))
        )
        for title, n in task_titles.items():
            if n >= min_count and title:
                signals.append({
                    "kind": "automation", "signal": "recurring_task",
                    "key": title, "count": n,
                    "detail": f"You created a task like “{title}” {n} times.",
                })

        # 2. repeated tool calls → shortcut candidate
        tool_calls = Counter(
            r["action"] for r in conn.execute(
                "SELECT action FROM audit_log WHERE ts>=? AND action LIKE 'tool.%'",
                (since,))
        )
        for action, n in tool_calls.items():
            if n >= min_count * 2:
                signals.append({
                    "kind": "habit", "signal": "frequent_tool",
                    "key": action, "count": n,
                    "detail": f"You used {action.removeprefix('tool.')} {n} times.",
                })

        # 3. frequent capture sources → watch/digest candidate
        sources = Counter(
            r["source"] for r in conn.execute(
                "SELECT source FROM memory_docs WHERE ts>=? AND kind='browser'",
                (since,))
        )
        for source, n in sources.items():
            if n >= min_count:
                signals.append({
                    "kind": "automation", "signal": "frequent_browsing",
                    "key": source, "count": n,
                    "detail": f"Frequent browsing activity from {source} ({n}).",
                })
    finally:
        conn.close()
    signals.sort(key=lambda s: -s["count"])
    return signals


def signal_evidence(signals: list[dict]) -> str:
    return json.dumps({"signals": signals}, separators=(",", ":"))
