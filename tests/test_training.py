import json

from maxel.llm import StubLLM
from maxel.local_runner import LocalStudio
from maxel.pricing import PRICING_SYSTEM, PricingRecommendation
from maxel.training import export_pricing, export_sections, pricing_seed, write_jsonl


def _valid_for_qwen_studio(rows):
    """Mirror of Qwen Studio's parse_data rules: system + alternating turns, distinct prompts, >= 10 rows."""
    seen = set()
    for r in rows:
        msgs = r["messages"]
        assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
        assert all(m["content"].strip() for m in msgs)
        key = json.dumps(msgs[:-1], sort_keys=True)
        assert key not in seen
        seen.add(key)
    assert len(rows) >= 10


def test_pricing_seed_is_valid_training_data():
    rows = pricing_seed(150)
    _valid_for_qwen_studio(rows)
    models = set()
    for r in rows:
        assert r["messages"][0]["content"] == PRICING_SYSTEM
        rec = PricingRecommendation.model_validate_json(r["messages"][2]["content"])  # every target is valid
        models.add(rec.model)
        assert len(json.dumps(r)) / 3.5 < 1024  # fits Qwen Studio's default max sequence length
    assert len(models) >= 4  # the seed covers most pricing models


def test_export_only_approved(studio, onboarded, tmp_path):
    studio.tools.recommend_pricing("qubit-sense")
    assert export_pricing(studio.store) == []
    key, result = studio.store.latest_pricing("qubit-sense")
    result.approved = True
    studio.store.put_pricing_at(key, result)
    assert len(export_pricing(studio.store)) == 1

    pid = LocalStudio(studio, StubLLM()).run("qubit-sense")
    meta = studio.store.get_meta("qubit-sense", pid)
    meta.approved = True
    studio.store.put_meta(meta)
    assert export_sections(studio.store, studio.config) == []  # stub output is never training data
    assert len(export_sections(studio.store, studio.config, include_stub=True)) == 8


def test_write_split(tmp_path):
    paths = write_jsonl(pricing_seed(20), tmp_path / "data", split=True)
    counts = [len(p.read_text().splitlines()) for p in paths]
    assert [p.name for p in paths] == ["train.jsonl", "valid.jsonl"] and counts == [16, 4]
