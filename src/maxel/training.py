"""Training data for the local Qwen model, in the JSONL format Qwen Studio and mlx_lm.lora accept:

    {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}

Two tasks, each with its prompt defined ONCE and shared with inference, so the model is
trained on exactly what it will be asked:

1. pricing  : startup profile -> pricing-model JSON          (short; fits max seq 1024)
2. sections : startup profile + section brief -> Markdown    (long; use max seq 2048)

Sources:
* `pricing_seed()`  synthetic profiles labelled by the rules baseline. Teaches the JSON schema
                    and sensible defaults. Bootstrap only: the model can at best copy the rules.
* `export_pricing()` pricing results a mentor approved (optionally edited). This is where the
                    model learns judgment beyond the rules.
* `export_sections()` sections from playbooks a mentor approved.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .models import SectionConfig, StartupProfile, StudioConfig
from .pricing import PRICING_SYSTEM, PROFILE_FIELDS, pricing_user, rules_recommend

SECTION_SYSTEM = (
    "You write one section of a go-to-market playbook for a deeptech startup in the Maxelerator "
    "accelerator. Use only facts from the profile; label any number you assume as a hypothesis "
    "and say how to validate it. Reply with the section body in Markdown, without a title."
)


def section_messages(section: SectionConfig, profile: StartupProfile | dict) -> tuple[str, str]:
    """Compact prompt for small local models (also used when exporting training data)."""
    data = profile.brief() if isinstance(profile, StartupProfile) else profile
    facts = "\n".join(f"{k}: {data.get(k)}" for k in (*PROFILE_FIELDS, "team", "goals_90d", "constraints") if data.get(k))
    user = f"SECTION: {section.id}\nTitle: {section.title}\nCover: {section.guidance.strip()}\n\nStartup profile\n{facts}"
    return SECTION_SYSTEM, user


def _row(system: str, user: str, assistant: str) -> dict:
    return {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user},
                         {"role": "assistant", "content": assistant}]}


def _compact(rec) -> str:
    return rec.model_dump_json(exclude_none=False)


# ---------------------------------------------------------------------------- synthetic seed

_SECTORS = [
    ("robotics", "Collaborative robots that {verb} {object}", "Plant managers at {buyer}", ["robot", "cobot"]),
    ("quantum sensing", "Quantum sensors that {verb} {object}", "Integrity engineers at {buyer}", ["sensor"]),
    ("bio-AI", "AI agents that {verb} {object}", "R&D heads at {buyer}", ["agent"]),
    ("climate", "Forecasting software that {verb} {object}", "Energy managers at {buyer}", ["forecast"]),
    ("materials", "A simulation platform that {verb} {object}", "Materials scientists at {buyer}", ["simulation"]),
    ("space", "Satellite instruments that {verb} {object}", "Analysts at {buyer}", ["satellite"]),
    ("medtech", "An imaging device that {verb} {object}", "Clinical leads at {buyer}", ["device"]),
    ("agtech", "Drones and models that {verb} {object}", "Agronomists at {buyer}", ["drone"]),
]
_VERBS = ["inspect", "screen", "optimise", "monitor", "design", "predict failures in", "measure", "map"]
_OBJECTS = ["welded joints", "antibody candidates", "battery fleets", "pipelines", "crop fields", "alloy compositions",
            "wind turbines", "chemical reactors", "water networks", "solar farms"]
_BUYERS = ["mid-size manufacturers", "utilities", "biotechs and CROs", "commercial real-estate portfolios",
           "midstream operators", "seed companies", "hospital networks", "chemical producers"]
_PRICING_TODAY = ["Not priced yet", "One paid pilot at ${k}k for {n} units", "${k}k per site per year (2 sites)",
                  "Considering per-run pricing", "Free design partners only", "Success fee on verified savings discussed",
                  "${k}k per seat per year quoted to one customer"]
_STAGES = ["idea", "pre-seed", "seed", "series-a"]


def synthetic_profiles(count: int, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    seen, out = set(), []
    while len(out) < count:
        sector, product, buyer_t, _ = rng.choice(_SECTORS)
        obj, verb, buyer = rng.choice(_OBJECTS), rng.choice(_VERBS), rng.choice(_BUYERS)
        today = rng.choice(_PRICING_TODAY).format(k=rng.choice([10, 25, 40, 60]), n=rng.choice([5, 10, 25, 50]))
        prof = {
            "name": f"{sector.split()[0].title()}{obj.split()[0].title()}{len(out)}",
            "sector": sector,
            "stage": rng.choice(_STAGES),
            "product": product.format(verb=verb, object=obj),
            "target_customers": buyer_t.format(buyer=buyer),
            "pricing_today": today,
            "traction": rng.choice(["None yet", "2 design partners", "1 paying customer", "3 LOIs"]),
            "sells_to_aws_customers": rng.random() < 0.3,
        }
        key = json.dumps({k: prof[k] for k in prof if k != "name"}, sort_keys=True)
        if key in seen:  # Qwen Studio rejects repeated prompts
            continue
        seen.add(key)
        out.append(prof)
    return out


def pricing_seed(count: int = 120, seed: int = 7) -> list[dict]:
    return [_row(PRICING_SYSTEM, pricing_user(p), _compact(rules_recommend(p))) for p in synthetic_profiles(count, seed)]


# ---------------------------------------------------------------------------- exports


def export_pricing(store, approved_only: bool = True) -> list[dict]:
    rows = []
    for _, result in store.list_pricing():
        if approved_only and not result.approved:
            continue
        profile = store.get_startup(result.startup_id)
        rows.append(_row(PRICING_SYSTEM, pricing_user(profile), _compact(result.recommendation)))
    return rows


def export_sections(store, config: StudioConfig, approved_only: bool = True, include_stub: bool = False) -> list[dict]:
    rows = []
    for profile in store.list_startups():
        for meta in store.list_playbooks(profile.startup_id):
            if meta.status != "complete" or (approved_only and not meta.approved):
                continue
            if not include_stub and meta.llm in (None, "StubLLM"):
                continue
            for sid, body in store.get_sections(profile.startup_id, meta.playbook_id).items():
                system, user = section_messages(config.section(sid), profile)
                rows.append(_row(system, user, body))
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """Keep the LAST answer per prompt (latest mentor edit wins); Qwen Studio rejects repeats."""
    by_prompt: dict[str, dict] = {}
    for r in rows:
        by_prompt[json.dumps(r["messages"][:-1], sort_keys=True)] = r
    return list(by_prompt.values())


def write_jsonl(rows: list[dict], out: Path, split: bool = False, seed: int = 42) -> list[Path]:
    """One file for Qwen Studio's upload, or train/valid files for `mlx_lm.lora --data <dir>`."""
    out = Path(out)
    rows = dedupe(rows)
    if not split:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return [out]
    out.mkdir(parents=True, exist_ok=True)
    items = list(rows)
    random.Random(seed).shuffle(items)
    n = max(2, round(len(items) * 0.2))
    paths = []
    for name, part in (("train", items[n:]), ("valid", items[:n])):
        p = out / f"{name}.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in part) + "\n")
        paths.append(p)
    return paths
