"""Calendar tool: reads ICS feeds (Google 'secret iCal address', Fastmail,
any .ics URL or local file) listed in config/jarvis.yaml. Minimal ICS
parsing in stdlib — events with DTSTART in the asked window. Recurring
events (RRULE) are NOT expanded (documented limitation; the base event's
date is used)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from jarvis.tools.registry import Tool, ToolContext, ToolError


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt, is_utc in (("%Y%m%dT%H%M%SZ", True), ("%Y%m%dT%H%M%S", False), ("%Y%m%d", False)):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if is_utc:
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        return dt
    return None


def parse_ics_events(text: str) -> list[dict]:
    events, current = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            if "start" in current:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, value = line.partition(":")
            key = key.split(";")[0].upper()
            if key == "DTSTART":
                current["start"] = _parse_dt(value)
                if current["start"] is None:
                    current.pop("start", None)
            elif key == "SUMMARY":
                current["summary"] = value.strip()
            elif key == "LOCATION":
                current["location"] = value.strip()
    return events


async def _load_feed(src: str) -> str:
    if src.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(src)
            resp.raise_for_status()
            return resp.text
    path = Path(src).expanduser()
    if path.is_file():
        return path.read_text(errors="replace")
    raise ToolError(f"calendar source unreadable: {src}")


async def calendar_events(args: dict, ctx: ToolContext) -> str:
    sources = ctx.cfg.tools.calendar_ics_urls
    if not sources:
        return ("No calendar configured. Add ICS URLs to tools.calendar_ics_urls "
                "in config/jarvis.yaml (Google Calendar → settings → 'secret "
                "address in iCal format').")
    days = int(args.get("days", 1))
    window_start = datetime.now().astimezone().replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
    window_end = window_start + timedelta(days=days)

    found = []
    for src in sources:
        try:
            for ev in parse_ics_events(await _load_feed(src)):
                start = ev["start"]
                if start.tzinfo is None:
                    start = start.astimezone()
                if window_start <= start < window_end:
                    found.append((start, ev.get("summary", "(untitled)"),
                                  ev.get("location", "")))
        except (httpx.HTTPError, ToolError) as exc:
            found.append((window_start, f"[calendar source failed: {exc}]", ""))
    if not found:
        return f"Nothing on the calendar in the next {days} day(s)."
    found.sort(key=lambda t: t[0])
    return "\n".join(
        f"{start.strftime('%a %H:%M')} {summary}" + (f" @ {loc}" if loc else "")
        for start, summary, loc in found
    )


TOOLS = [
    Tool(
        name="calendar_events",
        description="Upcoming calendar events. days: window size (default 1 = today).",
        params={"days": {"type": "integer"}},
        handler=calendar_events,
        permission="read",
    ),
]
