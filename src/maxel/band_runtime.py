"""BAND runtime: connects each studio role to BAND as a remote agent.

Each role is one BAND agent (one agent UUID + API key, created at app.band.ai) running the
Anthropic adapter, with the studio tools attached as custom tools. Agents coordinate in a
BAND room by @mentioning each other; a mentor or founder can join the same room at any time.

SDK surface used (band-sdk, see https://github.com/band-ai/band-sdk-python):
    from band import Agent
    from band.adapters import AnthropicAdapter, AnthropicAdapterConfig
    adapter = AnthropicAdapter(AnthropicAdapterConfig(model=..., custom_section=...),
                               additional_tools=[(InputModel, handler), ...])
    agent = Agent.create(adapter=adapter, agent_id=..., api_key=...)
    await agent.run()

Run ONE process per agent ID: two processes sharing credentials fight over the connection.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from pydantic import BaseModel

from . import tools as t
from .studio import Studio

log = logging.getLogger("maxel.band")

LEAD_ROLE = "strategist"


def _tool(model: type[BaseModel], fn):
    """Adapt a sync studio tool into an async BAND tool handler.

    Accepts the validated model instance, a dict, or keyword arguments, so it works across
    SDK versions. Errors come back as text, which lets the agent recover in the room.
    """

    async def handler(*args, **kwargs):
        if args and isinstance(args[0], model):
            data = args[0]
        elif args and isinstance(args[0], dict):
            data = model(**args[0])
        else:
            data = model(**{k: v for k, v in kwargs.items() if k in model.model_fields})
        try:
            return await asyncio.to_thread(fn, **data.model_dump())
        except t.InsufficientCredits as exc:
            return f"INSUFFICIENT CREDITS: {exc}"
        except Exception as exc:  # surface to the agent, keep the process alive
            log.exception("tool %s failed", model.__name__)
            return f"ERROR: {type(exc).__name__}: {exc}"

    handler.__name__ = f"maxel_{model.__name__}"
    return (model, handler)


def tools_for(studio: Studio, role: str) -> list:
    s = studio.tools
    common = [
        _tool(t.GetStartupProfile, s.get_startup_profile),
        _tool(t.ReadSections, s.read_sections),
        _tool(t.SaveSection, lambda **kw: s.save_section(agent=f"band:{role}", **kw)),
    ]
    if role == "pricing_architect":
        common.append(_tool(t.RecommendPricingModel, s.recommend_pricing))
    if role == LEAD_ROLE:
        common += [
            _tool(t.CheckCredits, s.check_credits),
            _tool(t.StartPlaybook, lambda startup_id: s.start_playbook(startup_id, mode="band")),
            _tool(t.FinalizePlaybook, s.finalize_playbook),
        ]
    return common


def credentials(studio: Studio, role: str) -> tuple[str, str]:
    prefix = studio.config.roles[role].env_prefix
    agent_id, api_key = os.environ.get(f"{prefix}_AGENT_ID"), os.environ.get(f"{prefix}_API_KEY")
    if not agent_id or not api_key:
        raise SystemExit(
            f"Missing {prefix}_AGENT_ID / {prefix}_API_KEY. Create the remote agent "
            f"'{studio.config.roles[role].display_name}' at app.band.ai (docs/BAND_SETUP.md)."
        )
    return agent_id, api_key


def build_agent(studio: Studio, role: str):
    try:
        from band import Agent
        from band.adapters import AnthropicAdapter, AnthropicAdapterConfig
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise SystemExit('BAND SDK not installed. Run: pip install -e ".[band]"  (Python 3.11+)') from exc

    agent_id, api_key = credentials(studio, role)
    adapter = AnthropicAdapter(
        AnthropicAdapterConfig(model=studio.settings.model, custom_section=studio.prompt(role)),
        additional_tools=tools_for(studio, role),
    )
    return Agent.create(adapter=adapter, agent_id=agent_id, api_key=api_key)


async def run_role(studio: Studio, role: str) -> None:
    if role not in studio.config.roles:
        raise SystemExit(f"unknown role '{role}'. Roles: {list(studio.config.roles)}")
    agent = build_agent(studio, role)
    log.info("starting BAND agent %s", studio.config.roles[role].display_name)
    await agent.run()


def run_all(studio: Studio) -> None:
    """Dev convenience: one child process per role (mirrors one container per role in prod)."""
    procs = [
        subprocess.Popen([sys.executable, "-m", "maxel.cli", "band", "run", "--role", role])
        for role in studio.config.roles
    ]
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


def kickoff_message(studio: Studio, startup_id: str) -> str:
    """The message a program manager posts in a BAND room to start a playbook."""
    lead = studio.config.roles[LEAD_ROLE].display_name
    others = ", ".join(f"@{r.display_name}" for k, r in studio.config.roles.items() if k != LEAD_ROLE)
    return (
        f"@{lead} please build the Maxelerator GTM playbook for startup `{startup_id}`. "
        f"The team in this room: {others}."
    )
