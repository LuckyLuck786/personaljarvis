"""Mini-only ('solo') operation: chat routes to the hub first (never waits on
a MacBook), and use_tools:false gives fast single-call chat."""

import pytest

from jarvis.core.bus import Bus
from jarvis.router.providers import ChatResult
from jarvis.router.router import ModelRouter, load_routing


def test_shipped_routing_is_hub_first():
    """The repo's default routing must send chat to the Mini first."""
    routing = load_routing("config/routing.yaml")
    assert routing["routes"]["chat"][0] == "hub_ollama"
    assert routing["routes"]["summarize"][0] == "hub_ollama"
    assert routing["routes"]["embed"] == ["hub_ollama"]


def test_default_config_is_mini_only():
    """Default jarvis.yaml ships with only the hub node active."""
    from jarvis.core.config import load_config

    cfg = load_config("config/jarvis.yaml")
    assert "hub_ollama" in cfg.nodes
    assert "macbook" not in cfg.nodes  # commented out by default
    assert cfg.cognition.use_tools is True


class StubProvider:
    def __init__(self, name):
        self.name = name
        self.calls = 0
        self.api_key = "k"

    async def chat(self, messages, model):
        self.calls += 1
        return ChatResult(text=f"from {self.name}", model=model, latency_ms=1.0)


async def test_chat_uses_hub_first_ignoring_absent_macbook(cfg):
    routing = load_routing("config/routing.yaml")
    # macbook node not configured (Mini-only) → tier should be skipped
    router = ModelRouter(cfg, routing, Bus(cfg.db_path), node_status={})
    router._providers = {"hub_ollama": StubProvider("hub_ollama")}
    r = await router.chat("chat", [{"role": "user", "content": "hi"}],
                          privacy_tags=("personal",))
    assert r.tier == "hub_ollama"  # answered locally, no MacBook involved


async def test_use_tools_false_gives_plain_chat(cfg):
    """With the tool-loop off, the agent makes a single router call."""
    from jarvis.cognition.agent import ChatAgent
    from jarvis.cognition.conversation import ConversationLog
    from jarvis.core.audit import AuditLog
    from jarvis.core.crypto import Vault
    from jarvis.core.killswitch import KillSwitch
    from jarvis.memory.embeddings import FakeEmbedder
    from jarvis.memory.store import MemoryStore
    from tests.conftest import FakeRouter, TEST_MASTER_KEY

    cfg.cognition.use_tools = False
    vault = Vault(TEST_MASTER_KEY)
    store = MemoryStore(cfg, vault, FakeEmbedder())
    router = FakeRouter("Plain answer, sir.")
    agent = ChatAgent(cfg, store, router, ConversationLog(cfg.db_path, vault),
                      KillSwitch(cfg.db_path), AuditLog(cfg.db_path),
                      toolloop=None)  # solo mode: no tool loop
    out = await agent.handle("hello", interface="test")
    assert out["reply"] == "Plain answer, sir."
    assert out["tools_used"] == []
    # plain mode: the system prompt has NO tool-calling protocol injected
    system = router.last_messages[0]["content"]
    assert "EXACTLY ONE JSON object" not in system
