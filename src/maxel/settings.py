"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    # Storage: "local" writes JSON/Markdown under data_dir; "s3" uses s3_bucket.
    store: str = "local"
    data_dir: Path = REPO_ROOT / "var"
    s3_bucket: str | None = None

    # Metering: "flexprice" sends events to FlexPrice; "log" writes a local ledger (offline/dev).
    meter: str = "log"
    flexprice_api_key: str | None = None
    flexprice_base_url: str = "https://us.api.flexprice.io/v1"
    flexprice_event_name: str = "maxel.credits"
    wallet_currency: str = "usd"

    # LLM used by the local runner: "anthropic" or "stub" (deterministic, for tests and demos).
    llm: str = "stub"
    anthropic_api_key: str | None = None
    model: str = "claude-sonnet-5-5"

    # Local / OpenAI-compatible model (e.g. Qwen served by mlx_lm.server)
    openai_base_url: str = "http://127.0.0.1:8081/v1"
    openai_model: str | None = None
    openai_api_key: str | None = None
    # Model that assigns pricing models: rules | openai (local Qwen) | anthropic | stub
    pricing_llm: str = "rules"

    # API
    admin_token: str | None = None

    config_path: Path = REPO_ROOT / "config" / "studio.yaml"
    prompts_dir: Path = REPO_ROOT / "prompts"
    templates_dir: Path = REPO_ROOT / "templates"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, dotenv: Path | None = REPO_ROOT / ".env") -> "Settings":
        if dotenv is not None:
            load_dotenv(dotenv)
        return cls(
            store=_env("MAXEL_STORE", "local"),
            data_dir=Path(_env("MAXEL_DATA_DIR", str(REPO_ROOT / "var"))),
            s3_bucket=_env("MAXEL_S3_BUCKET"),
            meter=_env("MAXEL_METER", "log"),
            flexprice_api_key=_env("FLEXPRICE_API_KEY"),
            flexprice_base_url=_env("FLEXPRICE_BASE_URL", "https://us.api.flexprice.io/v1"),
            flexprice_event_name=_env("FLEXPRICE_EVENT_NAME", "maxel.credits"),
            wallet_currency=_env("MAXEL_WALLET_CURRENCY", "usd"),
            llm=_env("MAXEL_LLM", "stub"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            model=_env("MAXEL_MODEL", "claude-sonnet-5-5"),
            openai_base_url=_env("MAXEL_OPENAI_BASE_URL", "http://127.0.0.1:8081/v1"),
            openai_model=_env("MAXEL_OPENAI_MODEL"),
            openai_api_key=_env("MAXEL_OPENAI_API_KEY"),
            pricing_llm=_env("MAXEL_PRICING_LLM", "rules"),
            admin_token=_env("MAXEL_ADMIN_TOKEN"),
            config_path=Path(_env("MAXEL_CONFIG", str(REPO_ROOT / "config" / "studio.yaml"))),
            prompts_dir=Path(_env("MAXEL_PROMPTS_DIR", str(REPO_ROOT / "prompts"))),
            templates_dir=Path(_env("MAXEL_TEMPLATES_DIR", str(REPO_ROOT / "templates"))),
        )
