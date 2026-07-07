"""Configuration: YAML (non-secret) + .env (secrets).

Precedence: environment variables > .env file > config/jarvis.yaml > defaults.
Everything host/port/path-related is config-driven so the system runs
identically split across nodes or collapsed onto one dev machine.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATHS = [
    Path("config/jarvis.yaml"),
    Path.home() / ".jarvis" / "jarvis.yaml",
]


class NodeConfig(BaseModel):
    ollama_url: str = ""
    role: str = ""
    check_interval_s: float = 30.0


class HubConfig(BaseModel):
    bind_host: str = "127.0.0.1"
    port: int = 8700
    rate_limit_per_min: int = 120


class BusConfig(BaseModel):
    poll_interval_s: float = 1.0
    max_attempts: int = 5
    retention_days: int = 14


class HealthConfig(BaseModel):
    history_rows_per_node: int = 500


class LoggingConfig(BaseModel):
    level: str = "INFO"
    pretty: bool = False


class ToolsConfig(BaseModel):
    # file_read/file_list may only touch paths under these roots
    files_allowed_roots: list[str] = Field(default_factory=list)
    # ICS URLs (or local .ics paths) the calendar tool reads
    calendar_ics_urls: list[str] = Field(default_factory=list)
    # home-lab nodes reachable via ssh for allow-listed commands: name -> user@host
    homelab: dict[str, str] = Field(default_factory=dict)


class CognitionConfig(BaseModel):
    # tool-loop on (natural-language reminders/tasks work) vs off (fast
    # single-call chat, better on a 6 GB Mini)
    use_tools: bool = True
    history_turns: int = 12


class Secrets(BaseSettings):
    """Secrets come exclusively from the environment / .env — never YAML."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jarvis_api_key: str = ""
    jarvis_master_key: str = ""
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    groq_api_key: str = ""
    cerebras_api_key: str = ""
    gemini_api_key: str = ""
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return "Secrets(<redacted>)"

    __str__ = __repr__


class Config(BaseModel):
    data_dir: Path
    hub: HubConfig = Field(default_factory=HubConfig)
    nodes: dict[str, NodeConfig] = Field(default_factory=dict)
    bus: BusConfig = Field(default_factory=BusConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    cognition: CognitionConfig = Field(default_factory=CognitionConfig)
    secrets: Secrets = Field(default_factory=Secrets, repr=False)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"


def _find_config_file(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    if env_path := os.environ.get("JARVIS_CONFIG"):
        return Path(env_path).expanduser()
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return candidate
    return None


def load_config(
    config_file: str | Path | None = None,
    env_file: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> Config:
    raw: dict = {}
    path = _find_config_file(config_file)
    if path and path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    resolved_data_dir = Path(
        data_dir
        or os.environ.get("JARVIS_DATA_DIR")
        or raw.get("data_dir")
        or Path.home() / ".jarvis"
    ).expanduser()
    resolved_data_dir.mkdir(parents=True, exist_ok=True)

    secrets = Secrets(_env_file=env_file) if env_file else Secrets()

    return Config(
        data_dir=resolved_data_dir,
        hub=HubConfig(**raw.get("hub", {})),
        nodes={k: NodeConfig(**v) for k, v in (raw.get("nodes") or {}).items()},
        bus=BusConfig(**raw.get("bus", {})),
        health=HealthConfig(**raw.get("health", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        tools=ToolsConfig(**raw.get("tools", {})),
        cognition=CognitionConfig(**raw.get("cognition", {})),
        secrets=secrets,
    )
