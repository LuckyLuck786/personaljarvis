"""Notes collector: watches directories of .md/.txt files (mtime+hash
polling — no native watcher dependency) and captures full note content on
create/change."""

from __future__ import annotations

from pathlib import Path

from jarvis.capture.base import CaptureEvent, Collector, content_hash

MAX_NOTE_CHARS = 100_000
EXTENSIONS = {".md", ".txt", ".org"}


class NotesCollector(Collector):
    name = "notes"
    kind = "note"

    async def poll(self) -> list[CaptureEvent]:
        seen: dict = self.state.data.setdefault("files", {})
        events = []
        for watch_dir in self.config.get("watch_dirs", []):
            root = Path(watch_dir).expanduser()
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not (path.is_file() and path.suffix.lower() in EXTENSIONS):
                    continue
                key = str(path)
                mtime = path.stat().st_mtime
                if seen.get(key, {}).get("mtime") == mtime:
                    continue
                try:
                    text = path.read_text(errors="replace")[:MAX_NOTE_CHARS]
                except OSError:
                    continue
                digest = content_hash(text)
                if seen.get(key, {}).get("hash") == digest:
                    seen[key] = {"mtime": mtime, "hash": digest}  # touch, same content
                    continue
                seen[key] = {"mtime": mtime, "hash": digest}
                if not text.strip():
                    continue
                events.append(CaptureEvent(
                    source=self.name, kind=self.kind,
                    content=self.redactor.redact(f"Note {path.name}:\n{text}"),
                    meta={"path": key},
                ))
        return events
