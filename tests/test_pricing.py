import json

import pytest
from pydantic import ValidationError

from maxel.llm import Completion, OpenAICompatLLM
from maxel.pricing import (
    PRICING_SYSTEM, PricingRecommendation, apply_steps, extract_json, flexprice_steps, pricing_user,
    recommend, rules_recommend,
)


class ScriptedLLM:
    """Plays back canned replies, recording what it was asked."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, user, max_tokens=4000):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply)


GOOD = {
    "model": "platform_plus_usage", "value_unit": "kilometre surveyed", "rationale": "Field cost plus value.",
    "billing_period": "ANNUAL", "currency": "usd", "platform_fee": 24000,
    "meter": {"event_name": "km_surveyed", "aggregation": "SUM", "field": "km"},
    "usage_price": {"billing_model": "TIERED", "tiers": [{"up_to": 100, "unit_amount": "1600"}, {"up_to": None, "unit_amount": "1200"}]},
    "pilot": {"fee": 40000, "weeks": 12, "success_criteria": "Wall-loss detection matches dig results"},
    "aws_marketplace_dimension": None,
}


def test_extract_json_from_chatty_reply():
    reply = 'Sure! Here it is:\n```json\n{"a": "brace } in \\"string\\"", "b": {"c": 1}}\n```\nThanks'
    assert extract_json(reply) == {"a": 'brace } in "string"', "b": {"c": 1}}
    with pytest.raises(ValueError):
        extract_json("no json here")


@pytest.mark.parametrize("patch, msg", [
    ({"meter": {"event_name": "KM Surveyed", "aggregation": "SUM", "field": "km"}}, "snake_case"),
    ({"meter": {"event_name": "km_surveyed", "aggregation": "SUM"}}, "needs a property"),
    ({"usage_price": {"billing_model": "TIERED", "tiers": [{"up_to": 100, "unit_amount": "1"}]}}, "last tier"),
    ({"usage_price": {"billing_model": "PACKAGE", "unit_amount": "10"}}, "package_size"),
    ({"platform_fee": None}, "needs platform_fee"),
    ({"model": "freemium"}, "model"),
])
def test_validation_rejects_bad_answers(patch, msg):
    with pytest.raises(ValidationError) as exc:
        PricingRecommendation.model_validate({**GOOD, **patch})
    assert msg in str(exc.value)


def test_model_answer_accepted(onboarded):
    llm = ScriptedLLM(json.dumps(GOOD))
    result = recommend(onboarded, llm)
    assert result.source == "model" and result.recommendation.platform_fee == 24000
    system, user = llm.calls[0]
    assert system == PRICING_SYSTEM and user == pricing_user(onboarded)
    assert "founder@" not in user  # contact details never reach the model


def test_retry_with_errors_then_accept(onboarded):
    bad = {**GOOD, "usage_price": {"billing_model": "TIERED", "tiers": [{"up_to": 100, "unit_amount": "1"}]}}
    llm = ScriptedLLM(json.dumps(bad), "```json\n" + json.dumps(GOOD) + "\n```")
    result = recommend(onboarded, llm)
    assert result.source == "model-retry" and result.errors
    assert "previous answer was invalid" in llm.calls[1][1]


def test_fallback_to_rules(onboarded):
    result = recommend(onboarded, ScriptedLLM("I think usage pricing!", "still not json"))
    assert result.source == "rules-fallback" and len(result.errors) == 2
    down = recommend(onboarded, ScriptedLLM(ConnectionError("server down"), ConnectionError("server down")))
    assert down.source == "rules-fallback"


def test_rules_use_known_pilot_price(profile):
    rec = rules_recommend(profile)  # "$40k for 25 km" pilot
    assert rec.model == "platform_plus_usage" and rec.pilot.fee == 40000
    assert rec.usage_price.tiers[0].unit_amount == "1600.00"


def test_flexprice_steps_and_apply():
    rec = PricingRecommendation.model_validate(GOOD)
    steps = flexprice_steps(rec, "QubitSense", "qubit-sense")
    assert [s["name"] for s in steps] == ["feature", "plan", "platform_fee", "usage_price", "pilot_plan", "pilot_fee"]
    feature = steps[0]["body"]
    assert feature["type"] == "metered" and feature["meter"]["aggregation"] == {"type": "SUM", "field": "km"}
    usage = steps[3]["body"]
    assert usage["invoice_cadence"] == "ARREAR" and usage["tier_mode"] == "SLAB" and usage["tiers"][-1]["up_to"] is None
    assert steps[2]["body"]["invoice_cadence"] == "ADVANCE" and steps[5]["body"]["billing_period"] == "ONETIME"

    class FakeClient:
        def __init__(self):
            self.sent = []

        def _request(self, method, path, body):
            self.sent.append((path, body))
            ids = {"/features": {"id": "feat_1", "meter": {"id": "meter_9"}}, "/plans": {"id": f"plan_{len(self.sent)}"}}
            return 201, ids.get(path, {"id": "price_x"})

    client = FakeClient()
    results = apply_steps(client, steps)
    assert results["feature"]["id"] == "feat_1"
    usage_sent = client.sent[3][1]
    assert usage_sent["meter_id"] == "meter_9" and usage_sent["entity_id"] == "plan_2"  # nested meter id resolved
    assert client.sent[5][1]["entity_id"] == "plan_5"


def test_openai_compat_request(monkeypatch):
    sent = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 1}}

    def fake_post(url, headers, json, timeout):
        sent.update(url=url, body=json)
        return Resp()

    monkeypatch.setattr("maxel.llm.requests.post", fake_post)
    out = OpenAICompatLLM("http://127.0.0.1:8081/v1").complete("sys", "user", max_tokens=50)
    assert out.text == "hi" and out.input_tokens == 7
    assert sent["url"] == "http://127.0.0.1:8081/v1/chat/completions"
    assert "model" not in sent["body"]  # mlx_lm.server uses the model it was started with
    assert sent["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_tool_saves_and_charges(studio, onboarded):
    out = studio.tools.recommend_pricing("qubit-sense")
    assert out.startswith("PRICING MODEL (assigned by rules")
    _, result = studio.store.latest_pricing("qubit-sense")
    assert result.recommendation.model == "platform_plus_usage"
    assert studio.meter.used("qubit-sense") == 5
