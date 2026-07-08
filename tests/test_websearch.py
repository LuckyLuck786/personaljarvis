import pytest

from jarvis.tools.websearch import _DDGParser, _decode_uddg, web_search
from tests.test_tools import make_registry

SAMPLE_DDG = """
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Ftopics%2Ftodo&rut=x">Todo topic · GitHub</a>
  <a class="result__snippet" href="...">A collection of todo list projects on GitHub.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftodomvc.com%2F">TodoMVC</a>
  <a class="result__snippet">Helping you select an MV* framework via todo apps.</a>
</div>
"""


def test_decode_uddg():
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&rut=abc"
    assert _decode_uddg(href) == "https://example.com/page"
    assert _decode_uddg("//example.com/x") == "https://example.com/x"
    assert _decode_uddg("https://direct.com") == "https://direct.com"


def test_ddg_parser_extracts_results():
    p = _DDGParser()
    p.feed(SAMPLE_DDG)
    results = p.results()
    assert len(results) == 2
    assert results[0]["url"] == "https://github.com/topics/todo"
    assert "GitHub" in results[0]["title"]
    assert "todo list projects" in results[0]["snippet"]
    assert results[1]["url"] == "https://todomvc.com/"


async def test_web_search_prefers_brave_when_keyed(cfg, monkeypatch):
    import jarvis.tools.websearch as ws

    cfg.secrets.brave_api_key = "brave-key"
    called = {}

    async def fake_brave(query, n, key):
        called["brave"] = (query, key)
        return [{"title": "Result", "url": "https://x.com", "snippet": "snip"}]

    monkeypatch.setattr(ws, "_brave", fake_brave)
    reg = make_registry(cfg)
    reg.discover()
    from jarvis.core.killswitch import KillSwitch

    out = await reg.execute("web_search", {"query": "todo apps"}, "test",
                            KillSwitch(cfg.db_path))
    assert called["brave"] == ("todo apps", "brave-key")
    assert "Result" in out and "https://x.com" in out


async def test_web_search_falls_back_to_ddg_keyless(cfg, monkeypatch):
    import jarvis.tools.websearch as ws

    # no keys set → DDG
    async def fake_ddg(query, n):
        return [{"title": "DDG hit", "url": "https://ddg.example", "snippet": "s"}]

    monkeypatch.setattr(ws, "_ddg", fake_ddg)
    reg = make_registry(cfg)
    reg.discover()
    from jarvis.core.killswitch import KillSwitch

    out = await reg.execute("web_search", {"query": "x"}, "t", KillSwitch(cfg.db_path))
    assert "DDG hit" in out


def test_web_search_tool_registered(cfg):
    reg = make_registry(cfg)
    reg.discover()
    assert "web_search" in reg.tools


async def test_extra_system_prompt_injected(cfg):
    from jarvis.cognition.agent import ChatAgent
    from jarvis.cognition.conversation import ConversationLog
    from jarvis.core.audit import AuditLog
    from jarvis.core.crypto import Vault
    from jarvis.core.killswitch import KillSwitch
    from jarvis.memory.embeddings import FakeEmbedder
    from jarvis.memory.store import MemoryStore
    from tests.conftest import FakeRouter, TEST_MASTER_KEY

    cfg.cognition.extra_system_prompt = "Be extremely thorough and detailed."
    vault = Vault(TEST_MASTER_KEY)
    store = MemoryStore(cfg, vault, FakeEmbedder())
    router = FakeRouter()
    agent = ChatAgent(cfg, store, router, ConversationLog(cfg.db_path, vault),
                      KillSwitch(cfg.db_path), AuditLog(cfg.db_path), toolloop=None)
    await agent.handle("hi", interface="test")
    assert "extremely thorough and detailed" in router.last_messages[0]["content"]
