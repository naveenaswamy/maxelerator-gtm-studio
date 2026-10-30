"""The studio's tools. The SAME functions back both runtimes:

* BAND mode: each agent gets these as custom tools (see band_runtime.py) and calls them itself.
* Local mode: local_runner.py calls them directly in a fixed order.

Every tool returns a short string, because that is what an LLM tool result is. Billing
happens here, so it is identical in both modes.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .metering import Meter
from .models import PlaybookMeta, StudioConfig, utcnow
from .playbook import render_playbook
from .store import BaseStore, NotFound


class InsufficientCredits(RuntimeError):
    pass


# --------------------------------------------------------------------------- tool inputs
# Pydantic models double as the JSON schema each BAND agent sees for the tool.


class GetStartupProfile(BaseModel):
    """Read a Maxelerator startup's intake profile (product, customers, stage, goals)."""

    startup_id: str = Field(description="The startup's slug, e.g. 'qubit-sense'")


class CheckCredits(BaseModel):
    """Check the startup's remaining GTM Studio credits and what a full playbook costs."""

    startup_id: str


class StartPlaybook(BaseModel):
    """Open a new playbook for a startup. Fails if the wallet cannot cover a full playbook.
    Returns the playbook_id that every later tool call needs."""

    startup_id: str


class ReadSections(BaseModel):
    """Read the sections already saved for a playbook, to keep new sections consistent."""

    startup_id: str
    playbook_id: str


class SaveSection(BaseModel):
    """Save one finished playbook section (Markdown). Charges the section's credits once."""

    startup_id: str
    playbook_id: str
    section_id: str = Field(description="One of the section ids you were assigned")
    markdown: str = Field(description="The full section body in Markdown, without a top-level title")


class RecommendPricingModel(BaseModel):
    """Assign a pricing model to the startup's own product using Maxelerator's pricing model
    (a local fine-tuned Qwen, or rules). Returns a validated recommendation and the FlexPrice
    setup (metered feature, plan, prices) to paste into the Pricing and packaging section."""

    startup_id: str


class FinalizePlaybook(BaseModel):
    """Assemble all saved sections into the finished playbook. Call only after every section is saved."""

    startup_id: str
    playbook_id: str


# --------------------------------------------------------------------------- implementation


def new_playbook_id() -> str:
    return datetime.now(timezone.utc).strftime("pb-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


@dataclass
class StudioTools:
    store: BaseStore
    meter: Meter
    config: StudioConfig
    templates_dir: object
    pricing_llm: object = None  # None = rules only; else an LLM (e.g. local Qwen via OpenAICompatLLM)

    # -- pricing-model assignment
    def recommend_pricing(self, startup_id: str) -> str:
        from .pricing import recommend, setup_markdown

        startup = self.store.get_startup(startup_id)
        result = recommend(startup, self.pricing_llm)
        key = self.store.put_pricing(result)
        self.meter.charge(
            startup, self.config.pricing_credits, action="pricing",
            event_id=f"pricing:{startup_id}:{result.created_at}", source=result.source,
        )
        note = {
            "model": "assigned by the pricing model",
            "model-retry": "assigned by the pricing model on its second try",
            "rules": "assigned by rules (no pricing model configured)",
            "rules-fallback": "the pricing model's answer failed validation, so rules assigned it",
        }[result.source]
        return (
            f"PRICING MODEL ({note}; saved to {self.store.locate(key)}):\n\n"
            f"{setup_markdown(result.recommendation)}\n\n"
            f"JSON:\n```json\n{result.recommendation.model_dump_json(indent=2)}\n```"
        )

    # -- reads
    def get_startup_profile(self, startup_id: str) -> str:
        try:
            profile = self.store.get_startup(startup_id)
        except NotFound as exc:
            return f"ERROR: {exc}"
        return json.dumps(profile.brief(), indent=2)

    def check_credits(self, startup_id: str) -> str:
        startup = self.store.get_startup(startup_id)
        balance = self.meter.balance(startup)
        cost = self.config.playbook_cost
        return json.dumps(
            {"startup_id": startup_id, "credits_available": balance, "playbook_cost": cost, "can_start": balance >= cost}
        )

    def read_sections(self, startup_id: str, playbook_id: str) -> str:
        sections = self.store.get_sections(startup_id, playbook_id)
        if not sections:
            return "No sections saved yet."
        titles = {s.id: s.title for s in self.config.sections}
        return "\n\n".join(f"### {titles.get(k, k)} ({k})\n{v}" for k, v in sections.items())

    # -- writes
    def start_playbook(self, startup_id: str, mode: str = "band") -> str:
        startup = self.store.get_startup(startup_id)
        balance = self.meter.balance(startup)
        cost = self.config.playbook_cost
        if balance < cost:
            raise InsufficientCredits(
                f"{startup.name} has {balance:g} credits; a playbook costs {cost:g}. Ask Maxelerator for a top-up."
            )
        meta = PlaybookMeta(playbook_id=new_playbook_id(), startup_id=startup_id, mode=mode)
        self.store.put_meta(meta)
        return meta.playbook_id

    def save_section(self, startup_id: str, playbook_id: str, section_id: str, markdown: str, agent: str = "") -> str:
        section = self.config.section(section_id)
        meta = self.store.get_meta(startup_id, playbook_id)
        if meta.status != "in_progress":
            return f"ERROR: playbook {playbook_id} is {meta.status}; start a new one."
        if not markdown.strip():
            return "ERROR: markdown is empty."

        startup = self.store.get_startup(startup_id)
        self.store.put_section(startup_id, playbook_id, section_id, markdown.strip())

        # One charge per (playbook, section): revisions of a section are free.
        charged = self.meter.charge(
            startup,
            section.credits,
            action="section",
            event_id=f"{playbook_id}:{section_id}",
            section=section_id,
            playbook_id=playbook_id,
            agent=agent or section.owner,
        )
        if section_id not in meta.sections_saved:
            meta.sections_saved.append(section_id)
        if charged:
            meta.credits_charged += section.credits
        self.store.put_meta(meta)

        remaining = [s.id for s in self.config.sections if s.id not in meta.sections_saved]
        note = f"charged {section.credits:g} credits" if charged else "revision saved, no extra charge"
        return f"SECTION SAVED: {section_id} ({note}). Remaining sections: {remaining or 'none'}"

    def finalize_playbook(self, startup_id: str, playbook_id: str) -> str:
        meta = self.store.get_meta(startup_id, playbook_id)
        sections = self.store.get_sections(startup_id, playbook_id)
        missing = [s.id for s in self.config.sections if s.id not in sections]
        if missing:
            return f"ERROR: cannot finalize; missing sections: {missing}"

        startup = self.store.get_startup(startup_id)
        markdown = render_playbook(self.templates_dir, self.config, startup, meta, sections)
        key = self.store.put_playbook_markdown(startup_id, playbook_id, markdown)

        charged = self.meter.charge(
            startup,
            self.config.finalize_credits,
            action="finalize",
            event_id=f"{playbook_id}:finalize",
            playbook_id=playbook_id,
        )
        if charged:
            meta.credits_charged += self.config.finalize_credits
        meta.status = "complete"
        meta.completed_at = utcnow()
        self.store.put_meta(meta)
        balance = self.meter.balance(startup)
        return (
            f"PLAYBOOK COMPLETE: {startup.name} ({playbook_id}). {meta.credits_charged:g} credits used, "
            f"{balance:g} remaining. Saved to {self.store.locate(key)}"
        )

    def fail_playbook(self, startup_id: str, playbook_id: str, error: str) -> None:
        meta = self.store.get_meta(startup_id, playbook_id)
        meta.status = "failed"
        meta.error = error[:500]
        self.store.put_meta(meta)
