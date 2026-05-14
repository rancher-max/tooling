from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_OUTPUT_DIR = "output"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    api_token: str
    output_dir: Path
    ollama_model: str
    ollama_host: str
    ollama_enabled: bool


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _normalize_ollama_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if not host:
        return DEFAULT_OLLAMA_HOST

    if "://" not in host:
        host = f"http://{host}"

    return host


def _ollama_host() -> str:
    # Prefer the new name while accepting the old variable during migration.
    host = _env("OLLAMA_HOST")
    if host:
        return _normalize_ollama_host(host)

    return _normalize_ollama_host(_env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_HOST))


def load_config() -> JiraConfig:
    load_dotenv(override=True)

    base_url = _env("JIRA_BASE_URL").rstrip("/")
    api_token = _env("JIRA_API_TOKEN")

    missing = [
        key
        for key, value in {
            "JIRA_BASE_URL": base_url,
            "JIRA_API_TOKEN": api_token,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    ollama_enabled_raw = _env("OLLAMA_ENABLED", "true").lower()
    ollama_enabled = ollama_enabled_raw not in {"0", "false", "no", "off"}

    return JiraConfig(
        base_url=base_url,
        api_token=api_token,
        output_dir=Path(_env("JIRA_EXTRACTOR_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)),
        ollama_model=_env("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        ollama_host=_ollama_host(),
        ollama_enabled=ollama_enabled,
    )
