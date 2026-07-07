"""Filesystem collector: records file create/modify activity under watched
directories. Always captures the path event; includes content only for
small text-like files. Ignore globs keep .git/node_modules noise out."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from jarvis.capture.base import CaptureEvent, Collector

MAX_CONTENT_CHARS = 32_000
TEXT_SUFFIXES = {".py", ".js", ".ts", ".md", ".txt", ".yaml", ".yml", ".json",
                 ".toml", ".sh", ".sql", ".html", ".css", ".rs", ".go", ".c",
                 ".cpp", ".java", ".swift"}


class FilesystemCollector(Collector):
    name = "filesystem"
    kind = "file"

    def _ignored(self, path: str) -> bool:
        return any(fnmatch(path, pat) for pat in self.config.get("ignore", []))

    async def poll(self) -> list[CaptureEvent]:
        seen: dict = self.state.data.setdefault("mtimes", {})
        first_run = not seen  # baseline pass: index without emitting a flood
        events = []
        for watch_dir in self.config.get("watch_dirs", []):
            root = Path(watch_dir).expanduser()
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                key = str(path)
                if not path.is_file() or self._ignored(key):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if seen.get(key) == mtime:
                    continue
                is_new = key not in seen
                seen[key] = mtime
                if first_run:
                    continue
                content = f"File {'created' if is_new else 'modified'}: {key}"
                if path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size < 200_000:
                    try:
                        body = path.read_text(errors="replace")[:MAX_CONTENT_CHARS]
                        content += f"\n\n{body}"
                    except OSError:
                        pass
                events.append(CaptureEvent(
                    source=self.name, kind=self.kind,
                    content=self.redactor.redact(content),
                    meta={"path": key, "new": is_new},
                ))
        return events
