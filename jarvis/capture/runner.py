"""Capture runner: loads config/capture.yaml, starts the enabled collectors,
and forwards their events to a sink (the hub's HTTP API from the MacBook, or
an in-process publisher when running on the hub itself)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import yaml

from jarvis.capture.base import Collector, HttpSink, Redactor
from jarvis.capture.browser import BrowserHistoryCollector
from jarvis.capture.clipboard import ClipboardCollector
from jarvis.capture.filesystem import FilesystemCollector
from jarvis.capture.notes import NotesCollector
from jarvis.capture.shell import ShellHistoryCollector
from jarvis.core.logging import get_logger

log = get_logger(__name__)

COLLECTORS: dict[str, type[Collector]] = {
    "notes": NotesCollector,
    "clipboard": ClipboardCollector,
    "filesystem": FilesystemCollector,
    "shell_history": ShellHistoryCollector,
    "browser_history": BrowserHistoryCollector,
    # 'screen_ocr' is config-schema'd but NOT implemented yet (Phase 2 optional
    # item, deferred): enabling it logs a truthful warning below.
}


def load_capture_config(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("JARVIS_CAPTURE", "config/capture.yaml"))
    return yaml.safe_load(path.read_text())


def build_collectors(capture_cfg: dict, state_dir: Path) -> list[Collector]:
    redactor = Redactor(capture_cfg.get("defaults", {}).get("redact_patterns", []))
    collectors = []
    for name, ccfg in capture_cfg.get("collectors", {}).items():
        if not ccfg.get("enabled", False):
            continue
        cls = COLLECTORS.get(name)
        if cls is None:
            log.warning("collector_not_implemented", collector=name)
            continue
        collectors.append(cls(ccfg, redactor, state_dir))
    return collectors


class CaptureRunner:
    def __init__(self, collectors: list[Collector], sink):
        self.collectors = collectors
        self.sink = sink
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if not self.collectors:
            log.warning("no_collectors_enabled",
                        hint="flip enabled: true in config/capture.yaml")
            return
        await asyncio.gather(
            *(c.run(self.sink, self._stop) for c in self.collectors)
        )

    def stop(self) -> None:
        self._stop.set()


def run_forever(hub_url: str, api_key: str, capture_config: str | None = None,
                state_dir: str | Path | None = None) -> None:
    """Entrypoint for `jarvis capture run` (MacBook / dev)."""
    capture_cfg = load_capture_config(capture_config)
    state_dir = Path(state_dir or Path.home() / ".jarvis" / "capture-state")
    collectors = build_collectors(capture_cfg, state_dir)
    log.info("capture_runner_starting",
             collectors=[c.name for c in collectors], hub=hub_url)
    runner = CaptureRunner(collectors, HttpSink(hub_url, api_key))
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        runner.stop()
