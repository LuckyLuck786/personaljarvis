import pytest

from jarvis.core.config import Config, HubConfig, Secrets
from jarvis.core.db import migrate
from jarvis.router.router import RouteResult

TEST_API_KEY = "test-api-key-not-secret"
# valid 32-byte urlsafe-b64 Fernet-style key, fixed for tests
TEST_MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class FakeRouter:
    """Canned-reply router that records the exact prompt it was given, so
    tests can assert what context (memories, history) reached the model."""

    def __init__(self, reply: str = "Understood."):
        self.reply = reply
        self.last_messages: list[dict] | None = None
        self.last_task: str | None = None
        self.last_privacy_tags: tuple = ()

    async def chat(self, task, messages, privacy_tags=()):
        self.last_task = task
        self.last_messages = messages
        self.last_privacy_tags = privacy_tags
        return RouteResult(self.reply, "fake_tier", "fake-model", 1.0)


@pytest.fixture()
def cfg(tmp_path) -> Config:
    c = Config(
        data_dir=tmp_path,
        hub=HubConfig(rate_limit_per_min=1000),
        nodes={},
        secrets=Secrets(
            _env_file=None,
            jarvis_api_key=TEST_API_KEY,
            jarvis_master_key=TEST_MASTER_KEY,
        ),
    )
    migrate(c.db_path)
    return c


@pytest.fixture()
def fake_router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture()
def client(cfg, fake_router):
    from fastapi.testclient import TestClient

    from jarvis.hub.app import create_app
    from jarvis.memory.embeddings import FakeEmbedder

    app = create_app(cfg, embedder=FakeEmbedder(), router=fake_router)
    with TestClient(app, headers={"x-api-key": TEST_API_KEY}) as c:
        yield c
