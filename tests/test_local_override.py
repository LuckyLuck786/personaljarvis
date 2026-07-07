"""Operator-local config overrides: config/<name>.local.yaml deep-merges on
top of the tracked config so operators tune their machine without git pain."""

from jarvis.core.config import _deep_merge, load_config, load_yaml_with_local
from jarvis.router.router import load_routing


def test_deep_merge_overlay_wins_nested():
    base = {"cognition": {"use_tools": True, "memory_k": 6}, "hub": {"port": 8700}}
    overlay = {"cognition": {"use_tools": False}}
    merged = _deep_merge(base, overlay)
    assert merged["cognition"]["use_tools"] is False   # overridden
    assert merged["cognition"]["memory_k"] == 6         # preserved
    assert merged["hub"]["port"] == 8700                # untouched


def test_load_yaml_with_local_merges(tmp_path):
    main = tmp_path / "jarvis.yaml"
    main.write_text("cognition:\n  use_tools: true\n  memory_k: 6\n")
    (tmp_path / "jarvis.local.yaml").write_text("cognition:\n  use_tools: false\n")
    data = load_yaml_with_local(main)
    assert data["cognition"]["use_tools"] is False
    assert data["cognition"]["memory_k"] == 6


def test_load_config_applies_local_override(tmp_path, monkeypatch):
    (tmp_path / "jarvis.yaml").write_text(
        "cognition:\n  use_tools: true\n  memory_k: 6\n"
    )
    (tmp_path / "jarvis.local.yaml").write_text(
        "cognition:\n  use_tools: false\n  memory_k: 3\n"
    )
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config(config_file=tmp_path / "jarvis.yaml")
    assert cfg.cognition.use_tools is False
    assert cfg.cognition.memory_k == 3


def test_routing_local_override(tmp_path):
    main = tmp_path / "routing.yaml"
    main.write_text(
        "tiers:\n"
        "  - name: hub_ollama\n    kind: ollama\n    node: hub_ollama\n"
        "    models: {chat: qwen2.5:3b, embed: nomic-embed-text}\n    privacy: local\n"
        "routes:\n  chat: [hub_ollama]\n  embed: [hub_ollama]\n"
    )
    (tmp_path / "routing.local.yaml").write_text(
        "tiers:\n"
        "  - name: hub_ollama\n    kind: ollama\n    node: hub_ollama\n"
        "    models: {chat: qwen2.5:1.5b, embed: nomic-embed-text}\n    privacy: local\n"
    )
    routing = load_routing(main)
    assert routing["tiers"][0]["models"]["chat"] == "qwen2.5:1.5b"  # override won
    assert routing["routes"]["chat"] == ["hub_ollama"]              # base kept


def test_no_local_file_is_fine(tmp_path):
    main = tmp_path / "jarvis.yaml"
    main.write_text("cognition:\n  use_tools: true\n")
    data = load_yaml_with_local(main)
    assert data["cognition"]["use_tools"] is True
