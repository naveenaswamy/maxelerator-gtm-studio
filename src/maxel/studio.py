"""Wires settings, storage, metering and tools together."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .metering import Meter, meter_from_settings
from .models import StudioConfig
from .settings import Settings
from .store import BaseStore, store_from_settings
from .tools import StudioTools


@dataclass
class Studio:
    settings: Settings
    config: StudioConfig
    store: BaseStore
    meter: Meter
    tools: StudioTools

    def prompt(self, role: str) -> str:
        """House rules + the role's own prompt, with section assignments filled in."""
        rules = (Path(self.settings.prompts_dir) / "_house_rules.md").read_text()
        role_cfg = self.config.roles[role]
        body = (Path(self.settings.prompts_dir) / role_cfg.prompt).read_text()
        owned = self.config.sections_for(role)
        assignments = "\n".join(f"- `{s.id}`: {s.title}. {s.guidance.strip()}" for s in owned) or "- none"
        team = "\n".join(f"- @{r.display_name} ({key})" for key, r in self.config.roles.items())
        all_sections = "\n".join(
            f"- `{s.id}` → @{self.config.roles[s.owner].display_name}{' (write last)' if s.final else ''}"
            for s in self.config.sections
        )
        return (
            f"{rules}\n\n{body}\n\n## Your sections\n{assignments}\n\n"
            f"## The team\n{team}\n\n## Section owners\n{all_sections}\n"
        )


def build_studio(settings: Settings | None = None) -> Studio:
    settings = settings or Settings.from_env()
    config = StudioConfig.load(settings.config_path)
    store = store_from_settings(settings)
    meter = meter_from_settings(settings)
    from .llm import llm_by_name

    pricing_llm = None if settings.pricing_llm == "rules" else llm_by_name(settings.pricing_llm, settings)
    tools = StudioTools(store=store, meter=meter, config=config, templates_dir=settings.templates_dir, pricing_llm=pricing_llm)
    return Studio(settings, config, store, meter, tools)


@lru_cache(maxsize=1)
def default_studio() -> Studio:
    return build_studio()
