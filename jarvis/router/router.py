"""Unified model router: picks a tier per task class from config/routing.yaml,
enforces the privacy boundary, and fails over down the chain automatically.

MacBook awake?  → its Ollama gets the request.
MacBook asleep? → cloud (or hub small model), no operator intervention.
Everything down → DegradedError; callers answer honestly from memory only.

Every request logs which tier served it (structlog + `llm.request` on the bus).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from jarvis.core.bus import Bus
from jarvis.core.config import Config
from jarvis.core.logging import get_logger
from jarvis.router.providers import (
    ChatResult,
    GeminiProvider,
    OllamaProvider,
    OpenAICompatProvider,
    ProviderError,
)

log = get_logger(__name__)

GROQ_BASE = "https://api.groq.com/openai/v1"
CEREBRAS_BASE = "https://api.cerebras.ai/v1"


class DegradedError(Exception):
    """Every eligible tier failed or was skipped."""

    def __init__(self, attempts: list[tuple[str, str]]):
        self.attempts = attempts
        super().__init__(f"all tiers failed/skipped: {attempts}")


@dataclass
class RouteResult:
    text: str
    tier: str
    model: str
    latency_ms: float


def load_routing(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("JARVIS_ROUTING", "config/routing.yaml"))
    # merge an optional operator-local override (config/routing.local.yaml)
    from jarvis.core.config import load_yaml_with_local

    return load_yaml_with_local(path)


class ModelRouter:
    def __init__(self, cfg: Config, routing: dict, bus: Bus,
                 node_status: dict[str, dict] | None = None):
        """node_status: live view from NodeMonitor.statuses (shared dict) —
        lets the router skip a tier the monitor already knows is down instead
        of eating a connect timeout."""
        self.cfg = cfg
        self.routing = routing
        self.bus = bus
        self.node_status = node_status if node_status is not None else {}
        self.tiers = {t["name"]: t for t in routing["tiers"]}
        s = cfg.secrets
        self._providers = {}
        for t in routing["tiers"]:
            kind = t["kind"]
            if kind == "ollama":
                node = cfg.nodes.get(t.get("node", ""))
                if node and node.ollama_url:
                    self._providers[t["name"]] = OllamaProvider(
                        node.ollama_url,
                        timeout_s=cfg.cognition.ollama_timeout_s,
                        num_predict=cfg.cognition.max_output_tokens,
                    )
            elif kind == "groq":
                self._providers[t["name"]] = OpenAICompatProvider(GROQ_BASE, s.groq_api_key)
            elif kind == "cerebras":
                self._providers[t["name"]] = OpenAICompatProvider(CEREBRAS_BASE, s.cerebras_api_key)
            elif kind == "gemini":
                self._providers[t["name"]] = GeminiProvider(s.gemini_api_key)

    # -- eligibility -----------------------------------------------------------

    def _skip_reason(self, tier: dict, privacy_tags: tuple[str, ...]) -> str | None:
        name = tier["name"]
        if name not in self._providers:
            return "not configured"
        if tier.get("privacy") == "cloud":
            pcfg = self.routing.get("privacy", {})
            local_only = set(pcfg.get("local_only_tags", []))
            if set(privacy_tags) & local_only and not pcfg.get("allow_cloud_for_tagged", False):
                return "privacy: local-only content"
            provider = self._providers[name]
            if not getattr(provider, "api_key", ""):
                return "no API key"
        if tier["kind"] == "ollama":
            status = self.node_status.get(tier.get("node", ""), {}).get("status")
            if status == "down":
                return "node down (monitor)"
        return None

    # -- routing ----------------------------------------------------------------

    async def chat(self, task: str, messages: list[dict],
                   privacy_tags: tuple[str, ...] = ()) -> RouteResult:
        order = self.routing["routes"].get(task) or self.routing["routes"]["chat"]
        attempts: list[tuple[str, str]] = []
        for tier_name in order:
            tier = self.tiers.get(tier_name)
            if tier is None:
                # a route may name a tier that isn't defined (e.g. a local
                # override trimmed the tiers list) — skip it, don't crash
                attempts.append((tier_name, "skipped: tier not defined"))
                continue
            if reason := self._skip_reason(tier, privacy_tags):
                attempts.append((tier_name, f"skipped: {reason}"))
                continue
            model = tier["models"].get(task) or tier["models"].get("chat")
            if not model:
                attempts.append((tier_name, "skipped: no model for task"))
                continue
            try:
                result: ChatResult = await self._providers[tier_name].chat(messages, model)
            except ProviderError as exc:
                attempts.append((tier_name, f"failed: {exc}"))
                log.warning("tier_failed", tier=tier_name, task=task, error=str(exc))
                continue
            log.info("llm_request", task=task, tier=tier_name, model=model,
                     latency_ms=result.latency_ms)
            self.bus.publish("llm.request", {
                "task": task, "tier": tier_name, "model": model,
                "latency_ms": result.latency_ms, "fallbacks": len(attempts),
            })
            return RouteResult(result.text, tier_name, model, result.latency_ms)

        log.error("all_tiers_failed", task=task, attempts=attempts)
        self.bus.publish("llm.request", {"task": task, "tier": None, "attempts": attempts})
        raise DegradedError(attempts)
