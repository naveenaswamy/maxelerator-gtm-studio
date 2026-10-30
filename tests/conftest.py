import json
from dataclasses import replace
from pathlib import Path

import pytest

from maxel.models import StartupProfile
from maxel.provisioning import onboard_startup
from maxel.settings import Settings
from maxel.studio import build_studio

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def studio(tmp_path):
    settings = replace(Settings.from_env(), data_dir=tmp_path / "var", store="local", meter="log", llm="stub", admin_token=None)
    return build_studio(settings)


@pytest.fixture
def profile():
    return StartupProfile(**json.loads((ROOT / "examples/startups/qubit-sense.json").read_text()))


@pytest.fixture
def onboarded(studio, profile):
    return onboard_startup(profile, studio.store, studio.meter, credits=500)
