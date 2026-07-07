"""Browser history collector: reads Chrome/Safari history SQLite databases
(via a temp copy — the live files are locked while the browser runs) and
captures new visits since the per-browser cursor. Excluded domains are
dropped at the source, before anything is stored."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import urlparse

from jarvis.capture.base import CaptureEvent, Collector

# Chrome epoch = 1601-01-01 in microseconds; Safari (WebKit) = 2001-01-01 in seconds
CHROME_EPOCH_OFFSET_US = 11_644_473_600_000_000
WEBKIT_EPOCH_OFFSET_S = 978_307_200

CHROME_HISTORY = "~/Library/Application Support/Google/Chrome/Default/History"
CHROME_HISTORY_LINUX = "~/.config/google-chrome/Default/History"
SAFARI_HISTORY = "~/Library/Safari/History.db"

MAX_VISITS_PER_POLL = 200


def _query_copy(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        shutil.copy2(db_path, tmp.name)
        conn = sqlite3.connect(tmp.name)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class BrowserHistoryCollector(Collector):
    name = "browser_history"
    kind = "browser"

    def _excluded(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return any(fnmatch(host, pat) for pat in self.config.get("exclude_domains", []))

    def _chrome_visits(self, path: Path, since_us: int) -> list[tuple[float, str, str]]:
        rows = _query_copy(
            path,
            "SELECT v.visit_time, u.url, u.title FROM visits v"
            " JOIN urls u ON u.id = v.url WHERE v.visit_time > ?"
            " ORDER BY v.visit_time LIMIT ?",
            (since_us, MAX_VISITS_PER_POLL),
        )
        return [((t - CHROME_EPOCH_OFFSET_US) / 1e6, url, title or "") for t, url, title in rows]

    def _safari_visits(self, path: Path, since_s: float) -> list[tuple[float, str, str]]:
        rows = _query_copy(
            path,
            "SELECT hv.visit_time, hi.url, hv.title FROM history_visits hv"
            " JOIN history_items hi ON hi.id = hv.history_item"
            " WHERE hv.visit_time > ? ORDER BY hv.visit_time LIMIT ?",
            (since_s, MAX_VISITS_PER_POLL),
        )
        return [(t + WEBKIT_EPOCH_OFFSET_S, url, title or "") for t, url, title in rows]

    async def poll(self) -> list[CaptureEvent]:
        cursors: dict = self.state.data.setdefault("cursors", {})
        events = []
        sources = []
        browsers = self.config.get("browsers", [])
        if "chrome" in browsers:
            for candidate in (CHROME_HISTORY, CHROME_HISTORY_LINUX):
                p = Path(candidate).expanduser()
                if p.is_file():
                    sources.append(("chrome", p))
                    break
        if "safari" in browsers:
            p = Path(SAFARI_HISTORY).expanduser()
            if p.is_file():
                sources.append(("safari", p))

        for browser, path in sources:
            try:
                if browser == "chrome":
                    cursor = cursors.get(browser)
                    if cursor is None:
                        # baseline at "now" in chrome-time; don't ingest years of history
                        import time as _t

                        cursors[browser] = int(_t.time() * 1e6) + CHROME_EPOCH_OFFSET_US
                        continue
                    visits = self._chrome_visits(path, int(cursor))
                    if visits:
                        cursors[browser] = int(max(v[0] for v in visits) * 1e6) + CHROME_EPOCH_OFFSET_US
                else:
                    cursor = cursors.get(browser)
                    if cursor is None:
                        import time as _t

                        cursors[browser] = _t.time() - WEBKIT_EPOCH_OFFSET_S
                        continue
                    visits = self._safari_visits(path, float(cursor))
                    if visits:
                        cursors[browser] = max(v[0] for v in visits) - WEBKIT_EPOCH_OFFSET_S
            except sqlite3.Error:
                continue  # locked/corrupt copy this round; retry next poll

            kept = [(ts, url, title) for ts, url, title in visits if not self._excluded(url)]
            if not kept:
                continue
            body = "\n".join(f"- {title} <{url}>" if title else f"- {url}"
                             for _, url, title in kept)
            events.append(CaptureEvent(
                source=self.name, kind=self.kind,
                content=self.redactor.redact(f"Browsing ({browser}):\n{body}"),
                meta={"browser": browser, "count": len(kept)},
                ts=kept[0][0],
            ))
        return events
