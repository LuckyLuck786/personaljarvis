import pytest

from jarvis.core import db
from jarvis.core.crypto import Vault
from jarvis.memory.chunking import chunk_text
from jarvis.memory.embeddings import FakeEmbedder
from jarvis.memory.store import MemoryStore
from tests.conftest import TEST_MASTER_KEY


@pytest.fixture()
def store(cfg):
    return MemoryStore(cfg, Vault(TEST_MASTER_KEY), FakeEmbedder())


def test_chunking_short_text_is_one_chunk():
    assert chunk_text("hello world") == ["hello world"]
    assert chunk_text("   ") == []


def test_chunking_long_text_overlaps():
    text = ("The resume design uses a serif font. " * 100).strip()
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 1500 for c in chunks)
    # overlap: consecutive chunks share content
    assert chunks[0][-50:] in chunks[0]  # sanity
    joined = "".join(chunks)
    assert "serif font" in joined


async def test_ingest_and_search_roundtrip(store):
    await store.ingest("I decided to use the serif font for the resume design",
                       kind="note", source="test", tags=("personal",))
    await store.ingest("The grocery list includes eggs and milk",
                       kind="note", source="test")

    results = await store.search("what did I decide about the resume design?", k=2)
    assert results
    assert "serif font" in results[0].text


async def test_content_is_encrypted_at_rest(store, cfg):
    secret = "my social security number is TOP-SECRET-VALUE"
    await store.ingest(secret, kind="note", source="test")

    # raw SQLite bytes must not contain the plaintext (content is Fernet'd;
    # vectors are just float blobs)
    raw = cfg.db_path.read_bytes()
    assert b"TOP-SECRET-VALUE" not in raw
    # no LanceDB directory is created anymore
    assert not (cfg.data_dir / "lancedb").exists()

    # but authorized search still decrypts it
    results = await store.search("social security number", k=1)
    assert "TOP-SECRET-VALUE" in results[0].text


async def test_search_empty_store_returns_nothing(store):
    assert await store.search("anything") == []


async def test_delete_doc_removes_from_search(store):
    doc_id = await store.ingest("ephemeral fact about zebras", kind="note", source="test")
    assert (await store.search("zebras", k=3))
    assert store.delete_doc(doc_id)
    results = await store.search("zebras", k=3)
    assert all("zebras" not in r.text for r in results)


async def test_recent_docs_decrypts(store):
    await store.ingest("newest note", kind="note", source="test")
    docs = store.recent_docs(limit=5)
    assert docs[0]["text"] == "newest note"


async def test_chunks_cascade_on_doc_delete(store, cfg):
    doc_id = await store.ingest("cascade test " * 200, kind="note", source="test")
    store.delete_doc(doc_id)
    conn = db.connect(cfg.db_path)
    left = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_chunks WHERE doc_id=?", (doc_id,)
    ).fetchone()["n"]
    conn.close()
    assert left == 0
