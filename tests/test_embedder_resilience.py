"""The always-on hub must not crash-loop on a minimal/botched config. A
common footgun: overwriting jarvis.yaml with an override that dropped the
`nodes:` block. The embedder should default to local Ollama instead."""

from jarvis.hub.app import DEFAULT_EMBED_MODEL, DEFAULT_OLLAMA_URL, _build_embedder


def _routing(node="hub_ollama", embed_model="nomic-embed-text"):
    return {
        "tiers": [{"name": "hub_ollama", "kind": "ollama", "node": node,
                   "models": {"embed": embed_model}}],
        "routes": {"embed": ["hub_ollama"]},
    }


def test_uses_configured_node(cfg):
    from jarvis.core.config import NodeConfig

    cfg.nodes = {"hub_ollama": NodeConfig(ollama_url="http://10.0.0.5:11434")}
    emb = _build_embedder(cfg, _routing())
    assert emb.base_url == "http://10.0.0.5:11434"
    assert emb.model == "nomic-embed-text"


def test_defaults_when_node_missing(cfg):
    """jarvis.yaml lost its nodes block → default to local Ollama, don't crash."""
    cfg.nodes = {}
    emb = _build_embedder(cfg, _routing())
    assert emb.base_url == DEFAULT_OLLAMA_URL
    assert emb.model == "nomic-embed-text"


def test_defaults_when_routing_malformed(cfg):
    cfg.nodes = {}
    emb = _build_embedder(cfg, {"tiers": [], "routes": {}})  # nothing usable
    assert emb.base_url == DEFAULT_OLLAMA_URL
    assert emb.model == DEFAULT_EMBED_MODEL


def test_hub_boots_with_minimal_config(cfg):
    """End-to-end: a config with no nodes still yields a working app instead
    of the create_app() crash that caused the systemd restart loop."""
    from fastapi.testclient import TestClient

    from jarvis.hub.app import create_app
    from tests.conftest import TEST_API_KEY, FakeRouter

    cfg.nodes = {}  # simulate the overwritten jarvis.yaml
    app = create_app(cfg, router=FakeRouter())  # no embedder passed → built from cfg
    with TestClient(app, headers={"x-api-key": TEST_API_KEY}) as client:
        assert client.get("/health").status_code == 200
