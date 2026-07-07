"""Phase 7: pattern mining is evidence-based (no hallucinated proposals),
sub-agents are tool-scoped, and the digital twin round-trips encrypted."""

import time

import pytest

from jarvis.advanced.improve import SelfImprovement
from jarvis.advanced.patterns import mine_signals
from jarvis.advanced.subagents import AGENT_PROFILES, SubAgentDispatcher, _ScopedRegistry
from jarvis.advanced.twin import DigitalTwin
from jarvis.core import db
from jarvis.core.audit import AuditLog
from jarvis.core.crypto import Vault
from jarvis.core.killswitch import KillSwitch
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.store import MemoryStore
from jarvis.router.router import RouteResult
from tests.conftest import TEST_MASTER_KEY
from tests.test_tools import make_registry, simple_tool


class StubRouter:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def chat(self, task, messages, privacy_tags=()):
        self.calls.append((task, messages))
        return RouteResult(self.reply, "stub", "m", 1.0)


def vault():
    return Vault(TEST_MASTER_KEY)


# -- pattern mining -------------------------------------------------------------

def test_mining_only_reports_real_repetition(cfg):
    conn = db.connect(cfg.db_path)
    now = time.time()
    for _ in range(4):  # repeated task title → signal
        conn.execute("INSERT INTO tasks (title, status, created_ts) VALUES"
                     " ('pay the electricity bill', 'open', ?)", (now,))
    conn.execute("INSERT INTO tasks (title, status, created_ts) VALUES"
                 " ('one-off thing', 'open', ?)", (now,))  # single → no signal
    conn.close()

    signals = mine_signals(cfg.db_path, min_count=3)
    keys = [s["key"] for s in signals]
    assert any("pay the electricity bill" in k for k in keys)
    assert not any("one-off thing" in k for k in keys)


def test_no_signals_no_proposals(cfg):
    signals = mine_signals(cfg.db_path)
    assert signals == []


async def test_proposals_grounded_in_signals(cfg):
    conn = db.connect(cfg.db_path)
    now = time.time()
    for _ in range(4):
        conn.execute("INSERT INTO tasks (title, status, created_ts) VALUES"
                     " ('water the plants', 'open', ?)", (now,))
    conn.close()

    router = StubRouter('[{"title":"Recurring: water the plants","rationale":'
                        '"You created this 4 times","kind":"automation"}]')
    imp = SelfImprovement(cfg, router, vault(), AuditLog(cfg.db_path))
    proposals = await imp.propose(min_count=3)
    assert proposals and proposals[0]["title"].startswith("Recurring")

    listed = imp.list_proposals("proposed")
    assert listed
    assert imp.set_status(listed[0]["id"], "dismissed")
    assert imp.list_proposals("proposed") == []


async def test_proposal_falls_back_without_llm(cfg):
    conn = db.connect(cfg.db_path)
    now = time.time()
    for _ in range(4):
        conn.execute("INSERT INTO tasks (title, status, created_ts) VALUES"
                     " ('renew ssl cert', 'open', ?)", (now,))
    conn.close()

    class DeadRouter:
        async def chat(self, *a, **k):
            raise RuntimeError("down")

    imp = SelfImprovement(cfg, DeadRouter(), vault(), AuditLog(cfg.db_path))
    proposals = await imp.propose(min_count=3)
    assert proposals  # still produces evidenced proposals from raw signals


def test_proposal_rationale_encrypted(cfg):
    conn = db.connect(cfg.db_path)
    v = vault()
    conn.execute("INSERT INTO improvement_proposals (kind, title, rationale_enc,"
                 " created_ts) VALUES ('automation', 't', ?, ?)",
                 (v.encrypt("memory", "SECRET-PATTERN detail"), time.time()))
    conn.close()
    assert b"SECRET-PATTERN" not in cfg.db_path.read_bytes()


# -- sub-agents -----------------------------------------------------------------

def test_scoped_registry_hides_other_tools(cfg):
    reg = make_registry(cfg, [simple_tool("web_fetch"), simple_tool("shell_exec"),
                              simple_tool("memory_search")])
    scoped = _ScopedRegistry(reg, ["web_fetch", "memory_search"])
    assert set(scoped.tools) == {"web_fetch", "memory_search"}
    assert "shell_exec" not in scoped.tools


async def test_scoped_registry_refuses_out_of_scope(cfg):
    reg = make_registry(cfg, [simple_tool("web_fetch"), simple_tool("shell_exec")])
    scoped = _ScopedRegistry(reg, ["web_fetch"])
    from jarvis.tools.registry import ToolError

    with pytest.raises(ToolError, match="not available to this sub-agent"):
        await scoped.execute("shell_exec", {}, "subagent", KillSwitch(cfg.db_path))


async def test_researcher_run_files_into_memory(cfg):
    import json

    store = MemoryStore(cfg, vault(), FakeEmbedder())
    reg = make_registry(cfg)
    reg.discover()
    # researcher just answers 'final' immediately for the test
    router = StubRouter(json.dumps({"action": "final", "reply": "Summary: X is Y."}))
    disp = SubAgentDispatcher(cfg, reg, router, KillSwitch(cfg.db_path), vault(),
                              store, AuditLog(cfg.db_path))
    run_id = disp.queue("researcher", "what is the capital of memory?")
    result = await disp.run(run_id)
    assert result["status"] == "done"

    got = await store.search("Summary X is Y", k=3)
    assert any("Summary" in r.text for r in got)  # research filed to memory


def test_unknown_agent_rejected(cfg):
    disp = SubAgentDispatcher(cfg, None, None, None, vault(), None,
                              AuditLog(cfg.db_path))
    with pytest.raises(ValueError):
        disp.queue("hacker", "do bad things")


# -- digital twin ---------------------------------------------------------------

async def test_twin_rebuild_and_prompt_block(cfg):
    store = MemoryStore(cfg, vault(), FakeEmbedder())
    await store.ingest("I'm building JARVIS and prefer lean systemd deploys",
                       kind="note", source="test", tags=("personal",))
    router = StubRouter("You are building JARVIS, a personal assistant. You "
                        "prefer lean deployments.\n- prefers systemd")
    twin = DigitalTwin(cfg, store, router, vault(), AuditLog(cfg.db_path))

    assert twin.current() is None
    assert twin.current_prompt_block() == ""  # nothing before a build

    out = await twin.rebuild()
    assert out["rebuilt"]
    block = twin.current_prompt_block()
    assert "OPERATOR CONTEXT" in block and "JARVIS" in block


async def test_twin_empty_when_no_material(cfg):
    store = MemoryStore(cfg, vault(), FakeEmbedder())
    twin = DigitalTwin(cfg, store, StubRouter("x"), vault(), AuditLog(cfg.db_path))
    out = await twin.rebuild()
    assert out["rebuilt"] is False


async def test_twin_summary_encrypted(cfg):
    store = MemoryStore(cfg, vault(), FakeEmbedder())
    await store.ingest("note about MY-PRIVATE-PROJECT", kind="note",
                       source="t", tags=("personal",))
    router = StubRouter("You work on MY-PRIVATE-PROJECT.")
    twin = DigitalTwin(cfg, store, router, vault(), AuditLog(cfg.db_path))
    await twin.rebuild()
    assert b"MY-PRIVATE-PROJECT" not in cfg.db_path.read_bytes()
