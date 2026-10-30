import asyncio
import json

import pytest

from maxel.band_runtime import kickoff_message, tools_for
from maxel.llm import StubLLM
from maxel.local_runner import LocalStudio
from maxel.provisioning import onboard_startup, top_up
from maxel.tools import InsufficientCredits, SaveSection


def test_config_cost(studio):
    # 7 sections x 10 + executive summary 15 + finalize 5 + pricing assignment 5
    assert studio.config.playbook_cost == 95


def test_onboard_is_idempotent(studio, profile):
    onboard_startup(profile, studio.store, studio.meter, credits=500)
    again = onboard_startup(profile.model_copy(update={"traction": "2 pilots"}), studio.store, studio.meter, credits=999)
    assert again.billing.initial_credits == 500
    assert studio.store.get_startup("qubit-sense").traction == "2 pilots"


def test_local_run_end_to_end(studio, onboarded):
    pid = LocalStudio(studio, StubLLM()).run("qubit-sense")
    meta = studio.store.get_meta("qubit-sense", pid)
    assert meta.status == "complete"
    assert meta.credits_charged == 95
    assert meta.llm == "StubLLM"
    md = studio.store.get_playbook_markdown("qubit-sense", pid)
    for s in studio.config.sections:
        assert s.title in md
    assert md.index("Executive summary") < md.index("Market and ideal customer profile")
    assert "founder@qubitsense.example" not in md  # contact details never reach the playbook
    assert json.loads(studio.tools.check_credits("qubit-sense"))["credits_available"] == 405


def test_section_revision_is_not_charged_twice(studio, onboarded):
    tools = studio.tools
    pid = tools.start_playbook("qubit-sense")
    first = tools.save_section("qubit-sense", pid, "market_icp", "v1")
    second = tools.save_section("qubit-sense", pid, "market_icp", "v2")
    assert "charged 10" in first and "no extra charge" in second
    assert studio.store.get_section("qubit-sense", pid, "market_icp") == "v2"
    assert studio.meter.used("qubit-sense") == 10


def test_finalize_refuses_missing_sections(studio, onboarded):
    pid = studio.tools.start_playbook("qubit-sense")
    assert studio.tools.finalize_playbook("qubit-sense", pid).startswith("ERROR: cannot finalize")


def test_unknown_section_rejected(studio, onboarded):
    pid = studio.tools.start_playbook("qubit-sense")
    with pytest.raises(KeyError):
        studio.tools.save_section("qubit-sense", pid, "not_a_section", "x")


def test_insufficient_credits_then_top_up(studio, profile):
    onboard_startup(profile, studio.store, studio.meter, credits=60)
    with pytest.raises(InsufficientCredits):
        studio.tools.start_playbook("qubit-sense")
    top_up("qubit-sense", 100, studio.store, studio.meter)
    assert studio.tools.start_playbook("qubit-sense").startswith("pb-")


def test_band_tool_handlers_without_sdk(studio, onboarded):
    """The BAND tool adapters work with model instances, dicts or kwargs."""
    lead = dict((m.__name__, h) for m, h in tools_for(studio, "strategist"))
    assert {"CheckCredits", "StartPlaybook", "FinalizePlaybook", "SaveSection"} <= set(lead)
    specialist = {m.__name__ for m, _ in tools_for(studio, "market_analyst")}
    assert "StartPlaybook" not in specialist and "FinalizePlaybook" not in specialist

    pid = asyncio.run(lead["StartPlaybook"](startup_id="qubit-sense"))
    out = asyncio.run(lead["SaveSection"](SaveSection(startup_id="qubit-sense", playbook_id=pid, section_id="market_icp", markdown="x")))
    assert out.startswith("SECTION SAVED: market_icp")
    out = asyncio.run(lead["SaveSection"]({"startup_id": "qubit-sense", "playbook_id": pid, "section_id": "bogus", "markdown": "x"}))
    assert out.startswith("ERROR: KeyError")


def test_band_insufficient_credits_message(studio, profile):
    onboard_startup(profile, studio.store, studio.meter, credits=10)
    lead = dict((m.__name__, h) for m, h in tools_for(studio, "strategist"))
    assert asyncio.run(lead["StartPlaybook"](startup_id="qubit-sense")).startswith("INSUFFICIENT CREDITS")


def test_prompt_contains_assignments(studio):
    p = studio.prompt("launch_planner")
    assert "`channels_aws`" in p and "`launch_90d`" in p and "house rules" in p.lower()
    assert "@Maxel Strategist" in kickoff_message(studio, "qubit-sense")
