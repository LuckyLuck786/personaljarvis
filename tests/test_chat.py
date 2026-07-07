"""End-to-end Phase 1 deliverable: 'Remember X' → later 'What about X?'
retrieves X and grounds the model prompt in it — via the real HTTP API,
memory store, and agent, with only the embedder and LLM faked."""


def test_remember_then_recall_grounds_the_prompt(client, fake_router):
    r = client.post("/chat", json={
        "text": "Remember: I decided to use the serif font for the resume design."
    })
    assert r.status_code == 200
    assert r.json()["tier"] == "fake_tier"

    r = client.post("/chat", json={
        "text": "What did I decide about the resume design font?"
    })
    assert r.status_code == 200
    assert r.json()["memories_used"] > 0

    # the retrieved memory must be in the system prompt, fenced as data
    system = fake_router.last_messages[0]
    assert system["role"] == "system"
    assert "serif font" in system["content"]
    assert "MEMORY SNIPPETS" in system["content"]


def test_conversation_history_carries_over(client, fake_router):
    client.post("/chat", json={"text": "My dog is called Biscuit.", "session": "s1"})
    client.post("/chat", json={"text": "Say that name back.", "session": "s1"})
    roles = [m["role"] for m in fake_router.last_messages]
    assert roles.count("user") >= 2  # prior turn included as history
    assert any("Biscuit" in m["content"] for m in fake_router.last_messages
               if m["role"] == "user")


def test_chat_is_privacy_tagged(client, fake_router):
    client.post("/chat", json={"text": "hello"})
    assert "personal" in fake_router.last_privacy_tags


def test_chat_refused_while_paused_with_honest_reply(client):
    client.post("/control/pause", json={"reason": "test"})
    # /chat itself is blocked by middleware (503) — belt
    assert client.post("/chat", json={"text": "hi"}).status_code == 503
    client.post("/control/resume")


def test_degraded_mode_is_announced(cfg):
    from fastapi.testclient import TestClient

    from jarvis.hub.app import create_app
    from jarvis.memory.embeddings import FakeEmbedder
    from jarvis.router.router import DegradedError

    class DeadRouter:
        async def chat(self, task, messages, privacy_tags=()):
            raise DegradedError([("all", "down")])

    app = create_app(cfg, embedder=FakeEmbedder(), router=DeadRouter())
    from tests.conftest import TEST_API_KEY

    with TestClient(app, headers={"x-api-key": TEST_API_KEY}) as c:
        r = c.post("/chat", json={"text": "hello?"})
        assert r.status_code == 200
        d = r.json()
        assert d["tier"] == "degraded"
        assert "degraded" in d["reply"].lower()


def test_chat_exchanges_are_audited(client):
    client.post("/chat", json={"text": "hello"})
    entries = client.get("/audit/tail").json()["entries"]
    assert any(e["action"] == "chat.message" for e in entries)


def test_memory_endpoints(client):
    r = client.post("/memory/ingest", json={"text": "standalone fact about llamas"})
    assert r.status_code == 200 and r.json()["doc_id"]
    r = client.get("/memory/search", params={"q": "llamas"})
    assert any("llamas" in res["text"] for res in r.json()["results"])
    r = client.get("/memory/recent", params={"limit": 5})
    assert r.json()["docs"]
