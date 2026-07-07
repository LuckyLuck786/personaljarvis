import time

from jarvis.core.crypto import Vault
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.graph import KnowledgeGraph, extract_entities
from jarvis.memory.store import MemoryStore
from tests.conftest import TEST_MASTER_KEY


# -- extraction -----------------------------------------------------------------

def test_extract_people_and_projects():
    ex = extract_entities("I met Sarah Chen about the Falcon project with @dave_ops")
    assert "Sarah Chen" in ex.people or "Sarah Chen" in ex.other
    assert "Falcon" in ex.projects
    assert "dave_ops" in ex.people


def test_extraction_ignores_sentence_openers_and_stopwords():
    ex = extract_entities("Today I will remember to call the plumber.")
    names = ex.people | ex.projects | ex.other
    assert "Today" not in names
    assert "I" not in names
    # 'call the plumber' is lowercase → not an entity
    assert not any(n.lower() == "plumber" for n in names)


def test_extraction_empty_on_plain_text():
    ex = extract_entities("the quick brown fox jumps over the lazy dog")
    assert not (ex.people | ex.projects | ex.other)


# -- graph store ----------------------------------------------------------------

def test_ingest_builds_cooccurrence_edges(cfg):
    g = KnowledgeGraph(cfg.db_path)
    g.ingest_text("Notes from the meeting: Alice Wong and Bob Reyes on Project Titan")
    stats = g.stats()
    assert stats["entities"] >= 2
    assert stats["relations"] >= 1  # they co-occur


def test_neighbors_after_repeated_cooccurrence(cfg):
    g = KnowledgeGraph(cfg.db_path)
    for _ in range(3):
        g.ingest_text("Alice Wong reviewed Project Titan again")
    g.ingest_text("Alice Wong also touched Project Nimbus")
    neighbors = g.neighbors("Alice Wong")
    names = [n["name"] for n in neighbors]
    assert "Project Titan" in " ".join(names) or "Titan" in " ".join(names)
    # Titan seen 3x with Alice → higher weight than Nimbus (1x)
    weights = {n["name"]: n["weight"] for n in neighbors}
    titan = next((v for k, v in weights.items() if "Titan" in k), 0)
    nimbus = next((v for k, v in weights.items() if "Nimbus" in k), 0)
    assert titan > nimbus


def test_unknown_entity_returns_empty(cfg):
    g = KnowledgeGraph(cfg.db_path)
    assert g.neighbors("Nonexistent Person") == []


def test_top_entities_ranked_by_degree(cfg):
    g = KnowledgeGraph(cfg.db_path)
    g.ingest_text("Alice Wong met Bob Reyes and Carol Diaz")  # Alice connects to 2
    g.ingest_text("Alice Wong met Dave Park")                  # Alice connects to 3
    top = g.top_entities(limit=5)
    assert top and top[0]["name"] == "Alice Wong"  # highest degree


async def test_graph_populated_through_memory_store(cfg):
    """The real integration: ingesting memory populates the graph."""
    g = KnowledgeGraph(cfg.db_path)
    store = MemoryStore(cfg, Vault(TEST_MASTER_KEY), FakeEmbedder(), graph=g)
    await store.ingest("Had lunch with Marcus Lee to discuss the Aurora project",
                       kind="note", source="test", tags=("personal",))
    assert g.stats()["entities"] >= 2
    neighbors = g.neighbors("Marcus Lee")
    assert any("Aurora" in n["name"] for n in neighbors)


async def test_backfill_from_existing_memory(cfg):
    # ingest WITHOUT a graph (simulating pre-graph data)...
    store_no_graph = MemoryStore(cfg, Vault(TEST_MASTER_KEY), FakeEmbedder())
    await store_no_graph.ingest("Working with Nina Patel on Project Zephyr",
                                kind="note", source="test", tags=("personal",))

    g = KnowledgeGraph(cfg.db_path)
    assert g.stats()["entities"] == 0  # graph empty despite the doc existing

    # ...then backfill
    store = MemoryStore(cfg, Vault(TEST_MASTER_KEY), FakeEmbedder(), graph=g)
    n = store.backfill_graph()
    assert n == 1
    assert g.stats()["entities"] >= 2


def test_graph_ingest_never_raises_on_garbage(cfg):
    g = KnowledgeGraph(cfg.db_path)
    # weird input must not throw (best-effort in the ingest path)
    assert g.ingest_text("") == 0
    assert g.ingest_text("!!!  @@@  ###") >= 0


def test_entity_names_only_no_content_leak(cfg):
    """The graph stores entity NAMES, not the surrounding sensitive content."""
    g = KnowledgeGraph(cfg.db_path)
    g.ingest_text("Alice Wong knows my PASSWORD-IS-hunter2 secret")
    raw = cfg.db_path.read_bytes()
    assert b"Alice Wong" in raw          # names are stored (low sensitivity)
    assert b"hunter2" not in raw         # content is NOT in the graph tables
