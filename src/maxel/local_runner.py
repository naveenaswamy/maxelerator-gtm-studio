"""Local runner: produces the same playbook as the BAND team, without BAND.

Use it for CI, dry runs (MAXEL_LLM=stub) and when a BAND workspace is not set up yet.
It plays each role in turn with that role's prompt and calls the same tools, so billing
and output are identical to BAND mode. BAND adds what this cannot: agents working in
parallel in a shared room, humans (mentors, founders) stepping in mid-run, and an audit
trail of every exchange.
"""

from __future__ import annotations

import json
import logging

from .llm import LLM, OpenAICompatLLM
from .pricing import setup_markdown
from .store import NotFound
from .studio import Studio
from .training import section_messages

log = logging.getLogger("maxel.local")

LOCAL_NOTE = (
    "\n\n## Local mode\nYou are running without BAND. Ignore instructions about @mentions and "
    "tools: reply with ONLY the Markdown body of the requested section, no title heading."
)


class LocalStudio:
    def __init__(self, studio: Studio, llm: LLM, compact: bool | None = None):
        self.studio = studio
        self.llm = llm
        # Small local models (e.g. Qwen 0.5B via mlx_lm.server) get the short prompt they were trained on.
        self.compact = isinstance(llm, OpenAICompatLLM) if compact is None else compact
        self.pricing_note = ""

    def _write(self, startup_id: str, playbook_id: str, section) -> None:
        tools = self.studio.tools
        if self.compact:
            system, user = section_messages(section, self.studio.store.get_startup(startup_id))
            if section.id == "pricing_packaging" and self.pricing_note:
                user += f"\n\nAssigned pricing model (use it):\n{self.pricing_note}"
            out = self.llm.complete(system, user, max_tokens=1500)
            self._save(startup_id, playbook_id, section, out)
            return
        brief = tools.get_startup_profile(startup_id)
        prior = tools.read_sections(startup_id, playbook_id)
        pricing = (
            f"\n\nPricing model assigned by RecommendPricingModel (build the section on it; "
            f"explain any change you make):\n{self.pricing_note}\n"
            if section.id == "pricing_packaging" and self.pricing_note else ""
        )
        user = (
            f"SECTION: {section.id}\n"
            f"Write the '{section.title}' section of the GTM playbook.\n\n"
            f"What it must cover: {section.guidance.strip()}\n\n"
            f"Startup profile:\n```json\n{brief}\n```\n\n"
            f"Sections already written (stay consistent with them):\n{prior}\n{pricing}"
        )
        system = self.studio.prompt(section.owner) + LOCAL_NOTE
        out = self.llm.complete(system, user)
        self._save(startup_id, playbook_id, section, out)

    def _save(self, startup_id, playbook_id, section, out) -> None:
        result = self.studio.tools.save_section(startup_id, playbook_id, section.id, out.text, agent=f"local:{section.owner}")
        if result.startswith("ERROR"):
            raise RuntimeError(result)
        log.info("%s | tokens in=%s out=%s", result, out.input_tokens, out.output_tokens)

    def run(self, startup_id: str, playbook_id: str | None = None) -> str:
        tools = self.studio.tools
        playbook_id = playbook_id or tools.start_playbook(startup_id, mode="local")
        meta = self.studio.store.get_meta(startup_id, playbook_id)
        meta.llm = type(self.llm).__name__
        self.studio.store.put_meta(meta)
        try:
            try:
                _, latest = self.studio.store.latest_pricing(startup_id)
            except NotFound:
                latest = None
            if latest is not None and latest.approved:
                # Reuse the mentor's decision: no new assignment, no pricing credits.
                self.pricing_note = f"PRICING MODEL (mentor-approved):\n\n{setup_markdown(latest.recommendation)}"
            else:
                self.pricing_note = tools.recommend_pricing(startup_id)
                meta = self.studio.store.get_meta(startup_id, playbook_id)
                meta.credits_charged += self.studio.config.pricing_credits
                self.studio.store.put_meta(meta)
            ordered =[s for s in self.studio.config.sections if not s.final] + [
                s for s in self.studio.config.sections if s.final
            ]
            for section in ordered:
                self._write(startup_id, playbook_id, section)
            result = tools.finalize_playbook(startup_id, playbook_id)
            if result.startswith("ERROR"):
                raise RuntimeError(result)
            log.info(result)
            return playbook_id
        except Exception as exc:
            tools.fail_playbook(startup_id, playbook_id, repr(exc))
            raise


def credits_summary(studio: Studio, startup_id: str) -> dict:
    return json.loads(studio.tools.check_credits(startup_id))
