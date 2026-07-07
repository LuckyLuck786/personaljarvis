"""Clipboard collector: polls the system clipboard (pbpaste on macOS,
xclip/wl-paste on Linux), hash-dedupes, redacts, captures."""

from __future__ import annotations

import asyncio
import sys

from jarvis.capture.base import CaptureEvent, Collector, content_hash

MAX_ITEM_CHARS = 64_000


async def read_clipboard() -> str | None:
    if sys.platform == "darwin":
        cmds = [["pbpaste"]]
    else:
        cmds = [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"]]
    for cmd in cmds:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            if proc.returncode == 0:
                return out.decode(errors="replace")
        except FileNotFoundError:
            continue
    return None


class ClipboardCollector(Collector):
    name = "clipboard"
    kind = "clipboard"

    async def poll(self) -> list[CaptureEvent]:
        text = await read_clipboard()
        if not text or not text.strip():
            return []
        text = text[: int(self.config.get("max_item_kb", 64)) * 1000]
        digest = content_hash(text)
        if self.state.data.get("last_hash") == digest:
            return []
        self.state.data["last_hash"] = digest
        return [CaptureEvent(
            source=self.name, kind=self.kind,
            content=self.redactor.redact(f"Clipboard:\n{text[:MAX_ITEM_CHARS]}"),
            meta={"chars": len(text)},
        )]
