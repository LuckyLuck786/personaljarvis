import pytest

from jarvis.core.config import Config, HubConfig, Secrets
from jarvis.core.db import migrate

TEST_API_KEY = "test-api-key-not-secret"
# valid 32-byte urlsafe-b64 Fernet-style key, fixed for tests
TEST_MASTER_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


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
def client(cfg):
    from fastapi.testclient import TestClient

    from jarvis.hub.app import create_app

    app = create_app(cfg)
    with TestClient(app, headers={"x-api-key": TEST_API_KEY}) as c:
        yield c
