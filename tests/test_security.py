from fastapi.testclient import TestClient

from jarvis.core.config import Config, HubConfig, Secrets
from jarvis.core.db import migrate
from jarvis.hub.app import create_app
from tests.conftest import TEST_API_KEY, TEST_MASTER_KEY


def test_no_key_is_rejected(client):
    r = client.get("/health", headers={"x-api-key": ""})
    assert r.status_code == 401


def test_wrong_key_is_rejected(client):
    r = client.get("/health", headers={"x-api-key": "wrong-key"})
    assert r.status_code == 401


def test_every_endpoint_requires_auth(client):
    for path in ("/health", "/control/state", "/bus/tail", "/audit/tail"):
        assert client.get(path, headers={"x-api-key": ""}).status_code == 401


def test_valid_key_accepted(client):
    assert client.get("/health").status_code == 200


def test_hub_refuses_to_start_without_api_key(tmp_path):
    import pytest

    cfg = Config(
        data_dir=tmp_path,
        secrets=Secrets(_env_file=None, jarvis_api_key="", jarvis_master_key=TEST_MASTER_KEY),
    )
    migrate(cfg.db_path)
    with pytest.raises(ValueError, match="JARVIS_API_KEY"):
        create_app(cfg)


def test_rate_limit_kicks_in(tmp_path):
    cfg = Config(
        data_dir=tmp_path,
        hub=HubConfig(rate_limit_per_min=5),
        secrets=Secrets(
            _env_file=None, jarvis_api_key=TEST_API_KEY, jarvis_master_key=TEST_MASTER_KEY
        ),
    )
    migrate(cfg.db_path)
    app = create_app(cfg)
    with TestClient(app, headers={"x-api-key": TEST_API_KEY}) as c:
        statuses = [c.get("/control/state").status_code for _ in range(8)]
    assert statuses.count(200) == 5
    assert statuses.count(429) == 3


def test_bus_publish_validates_topic(client):
    r = client.post("/bus/publish", json={"topic": "DROP TABLE; --", "payload": {}})
    assert r.status_code == 422
