"""Shell history collector: tails zsh/bash history files incrementally by
byte offset. Understands zsh extended history (`: <ts>:<dur>;<cmd>`) and
plain formats. Commands are batched into one event per poll to keep the
memory store from filling with single-line docs."""

from __future__ import annotations

import re
import time
from pathlib import Path

from jarvis.capture.base import CaptureEvent, Collector

ZSH_EXT_RE = re.compile(r"^: (\d+):\d+;(.*)$")
MAX_BATCH_CHARS = 20_000


def parse_history_lines(lines: list[str]) -> list[tuple[float | None, str]]:
    """→ [(timestamp | None, command)]; handles zsh multi-line continuations."""
    out: list[tuple[float | None, str]] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        m = ZSH_EXT_RE.match(line)
        if m:
            out.append((float(m.group(1)), m.group(2)))
        elif out and out[-1][1].endswith("\\"):
            ts, prev = out[-1]
            out[-1] = (ts, prev[:-1] + "\n" + line)
        else:
            out.append((None, line))
    return out


class ShellHistoryCollector(Collector):
    name = "shell_history"
    kind = "shell"

    async def poll(self) -> list[CaptureEvent]:
        offsets: dict = self.state.data.setdefault("offsets", {})
        events = []
        for hist_file in self.config.get("files", []):
            path = Path(hist_file).expanduser()
            if not path.is_file():
                continue
            key = str(path)
            size = path.stat().st_size
            offset = offsets.get(key)
            if offset is None:
                offsets[key] = size  # first run: baseline at EOF, don't ingest years of history
                continue
            if size < offset:
                offset = 0  # history file was rotated/truncated
            if size == offset:
                continue
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read().decode(errors="replace")
            offsets[key] = size
            commands = parse_history_lines(chunk.splitlines())
            if not commands:
                continue
            body = "\n".join(cmd for _, cmd in commands)[:MAX_BATCH_CHARS]
            first_ts = next((ts for ts, _ in commands if ts), time.time())
            events.append(CaptureEvent(
                source=self.name, kind=self.kind,
                content=self.redactor.redact(
                    f"Shell commands ({path.name}):\n{body}"
                ),
                meta={"file": key, "count": len(commands)},
                ts=first_ts,
            ))
        return events
