"""Data models: startup profiles, studio configuration, playbooks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- startups


class Billing(BaseModel):
    """How a startup is linked to FlexPrice. external_id is the join key for every usage event."""

    external_id: str
    flexprice_customer_id: str | None = None
    wallet_id: str | None = None
    initial_credits: float = 0


class StartupProfile(BaseModel):
    """The intake form a founder fills in. Agents read only this record (plus prior sections)."""

    startup_id: str = Field(description="URL-safe slug, e.g. 'qubit-sense'")
    name: str
    one_liner: str
    sector: str = Field(description="Deeptech sector: robotics, bio-AI, quantum, climate, materials, space, ...")
    stage: Literal["idea", "pre-seed", "seed", "series-a", "later"] = "pre-seed"
    product: str
    target_customers: str
    pricing_today: str = "Not priced yet"
    traction: str = "None yet"
    team: str = ""
    goals_90d: str = ""
    constraints: str = ""
    founder_email: str | None = None
    sells_to_aws_customers: bool = Field(
        default=False, description="True when buyers hold AWS committed spend (AWS Marketplace is a fit)"
    )
    billing: Billing | None = None
    created_at: str = Field(default_factory=utcnow)

    @field_validator("startup_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError("startup_id must be lowercase letters, digits and dashes (2-63 chars)")
        return v

    def brief(self) -> dict:
        """The fields agents see. Billing and contact details stay out of prompts."""
        return self.model_dump(exclude={"billing", "founder_email", "created_at"})


# --------------------------------------------------------------------------- studio config


class RoleConfig(BaseModel):
    display_name: str
    env_prefix: str
    prompt: str


class SectionConfig(BaseModel):
    id: str
    title: str
    owner: str
    credits: float
    guidance: str
    final: bool = False


class StudioConfig(BaseModel):
    name: str
    default_credits_per_startup: float
    finalize_credits: float
    pricing_credits: float = 5
    roles: dict[str, RoleConfig]
    sections: list[SectionConfig]

    @classmethod
    def load(cls, path: Path) -> "StudioConfig":
        raw = yaml.safe_load(Path(path).read_text())
        studio = raw["studio"]
        cfg = cls(
            name=studio["name"],
            default_credits_per_startup=studio["default_credits_per_startup"],
            finalize_credits=studio["finalize_credits"],
            pricing_credits=studio.get("pricing_credits", 5),
            roles=raw["roles"],
            sections=raw["sections"],
        )
        unknown = {s.owner for s in cfg.sections} - set(cfg.roles)
        if unknown:
            raise ValueError(f"sections reference unknown roles: {sorted(unknown)}")
        return cfg

    def section(self, section_id: str) -> SectionConfig:
        for s in self.sections:
            if s.id == section_id:
                return s
        raise KeyError(f"unknown section '{section_id}'. Valid: {[s.id for s in self.sections]}")

    @property
    def playbook_cost(self) -> float:
        return sum(s.credits for s in self.sections) + self.finalize_credits + self.pricing_credits

    def sections_for(self, role: str) -> list[SectionConfig]:
        return [s for s in self.sections if s.owner == role]


# --------------------------------------------------------------------------- playbooks


class PlaybookMeta(BaseModel):
    playbook_id: str
    startup_id: str
    status: Literal["in_progress", "complete", "failed"] = "in_progress"
    mode: Literal["local", "band"] = "local"
    sections_saved: list[str] = Field(default_factory=list)
    credits_charged: float = 0
    created_at: str = Field(default_factory=utcnow)
    completed_at: str | None = None
    error: str | None = None
    llm: str | None = None  # which model wrote the sections (training export skips stub output)
    approved: bool = False  # set by a mentor; only approved playbooks become training data
