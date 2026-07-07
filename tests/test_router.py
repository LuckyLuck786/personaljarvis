import pytest

from jarvis.core.bus import Bus
from jarvis.router.providers import ChatResult, ProviderError
from jarvis.router.router import DegradedError, ModelRouter

ROUTING = {
    "tiers": [
        {"name": "macbook_ollama", "kind": "ollama", "node": "macbook",
         "models": {"chat": "qwen2.5:14b"}, "privacy": "local"},
        {"name": "hub_ollama", "kind": "ollama", "node": "hub_ollama",
         "models": {"chat": "llama3.2:3b", "embed": "nomic-embed-text"},
         "privacy": "local"},
        {"name": "groq", "kind": "groq", "models": {"chat": "llama-3.3-70b"},
         "privacy": "cloud"},
    ],
    "routes": {
        "chat": ["macbook_ollama", "groq", "hub_ollama"],
        "embed": ["hub_ollama"],
    },
    "privacy": {"local_only_tags": ["personal"], "allow_cloud_for_tagged": False},
}


class StubProvider:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.calls = 0
        self.api_key = "stub-key"

    async def chat(self, messages, model):
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.name} unavailable")
        return ChatResult(text=f"reply from {self.name}", model=model, latency_ms=1.0)


def make_router(cfg, node_status=None, **stub_flags):
    import copy

    router = ModelRouter(cfg, copy.deepcopy(ROUTING), Bus(cfg.db_path),
                         node_status=node_status or {})
    stubs = {}
    for name in ("macbook_ollama", "hub_ollama", "groq"):
        stubs[name] = StubProvider(name, fail=stub_flags.get(f"{name}_fails", False))
    router._providers = stubs
    return router, stubs


async def test_primary_tier_serves(cfg):
    router, stubs = make_router(cfg)
    r = await router.chat("chat", [{"role": "user", "content": "hi"}])
    assert r.tier == "macbook_ollama"
    assert stubs["groq"].calls == 0


async def test_macbook_down_fails_over(cfg):
    """The core Phase 1 guarantee: laptop asleep → next tier, automatically."""
    router, stubs = make_router(cfg, macbook_ollama_fails=True)
    r = await router.chat("chat", [{"role": "user", "content": "hi"}])
    assert r.tier == "groq"
    assert stubs["macbook_ollama"].calls == 1


async def test_monitor_known_down_is_skipped_without_a_call(cfg):
    router, stubs = make_router(cfg, node_status={"macbook": {"status": "down"}})
    r = await router.chat("chat", [{"role": "user", "content": "hi"}])
    assert r.tier == "groq"
    assert stubs["macbook_ollama"].calls == 0  # no timeout wasted


async def test_privacy_tagged_content_never_reaches_cloud(cfg):
    router, stubs = make_router(cfg, macbook_ollama_fails=True)
    r = await router.chat("chat", [{"role": "user", "content": "hi"}],
                          privacy_tags=("personal",))
    assert r.tier == "hub_ollama"      # skipped groq entirely
    assert stubs["groq"].calls == 0


async def test_all_tiers_down_raises_degraded(cfg):
    router, _ = make_router(cfg, macbook_ollama_fails=True, groq_fails=True,
                            hub_ollama_fails=True)
    with pytest.raises(DegradedError) as exc:
        await router.chat("chat", [{"role": "user", "content": "hi"}])
    assert len(exc.value.attempts) == 3


async def test_tier_choice_is_logged_to_bus(cfg):
    router, _ = make_router(cfg)
    bus = Bus(cfg.db_path)
    await router.chat("chat", [{"role": "user", "content": "hi"}])
    events = bus.tail("llm.request")
    assert events and events[-1].payload["tier"] == "macbook_ollama"
