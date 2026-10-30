"""Pricing-model assignment: a startup profile in, a validated pricing model and FlexPrice setup out.

This is the job the local Qwen model does. A 0.5B model cannot write a whole playbook well,
but it can learn one narrow structured task: read a short profile and emit a JSON pricing
model. Every answer is validated; an invalid answer gets one retry with the validation errors,
then falls back to deterministic rules, so a bad generation never reaches FlexPrice.

The recommendation becomes FlexPrice objects in the STARTUP's own FlexPrice account
(their API key, not Maxelerator's): one metered feature, a plan, and its prices. Verified
API shape (October 2026): features carry a `meter` (event_name, aggregation {type, field},
reset_usage); prices are created separately with entity_type=PLAN; usage prices need
meter_id and invoice_cadence ARREAR; TIERED needs tier_mode; PACKAGE needs transform_quantity.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .models import StartupProfile, utcnow

PricingModelName = Literal["usage", "prepaid_credits", "platform_plus_usage", "seat_plus_usage", "outcome_based"]
Aggregation = Literal["COUNT", "SUM", "MAX", "COUNT_UNIQUE", "LATEST"]

# ---------------------------------------------------------------------------- schema


class Tier(BaseModel):
    up_to: int | None = Field(description="Upper bound of the tier in value units; null on the last tier")
    unit_amount: str = Field(description="Price per unit in this tier, as a decimal string")

    @field_validator("unit_amount", mode="before")
    @classmethod
    def _amount(cls, v):
        v = str(v)
        if float(v) < 0:
            raise ValueError("unit_amount must be >= 0")
        return v


class Meter(BaseModel):
    event_name: str
    aggregation: Aggregation
    field: str | None = Field(default=None, description="Event property to aggregate; not used by COUNT")

    @field_validator("event_name")
    @classmethod
    def _event(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_.]{2,49}", v):
            raise ValueError("event_name must be lowercase snake_case, 3-50 chars")
        return v

    @model_validator(mode="after")
    def _field(self):
        if self.aggregation != "COUNT" and not self.field:
            raise ValueError(f"aggregation {self.aggregation} needs a property `field`")
        return self


class UsagePrice(BaseModel):
    billing_model: Literal["FLAT_FEE", "PACKAGE", "TIERED"]
    unit_amount: str | None = Field(default=None, description="Per unit (FLAT_FEE) or per package (PACKAGE)")
    package_size: int | None = None
    tiers: list[Tier] | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.billing_model in ("FLAT_FEE", "PACKAGE"):
            if self.unit_amount is None or float(self.unit_amount) < 0:
                raise ValueError(f"{self.billing_model} needs unit_amount >= 0")
            self.unit_amount = str(self.unit_amount)
        if self.billing_model == "PACKAGE" and not (self.package_size and self.package_size > 0):
            raise ValueError("PACKAGE needs package_size > 0")
        if self.billing_model == "TIERED":
            if not self.tiers:
                raise ValueError("TIERED needs tiers")
            if self.tiers[-1].up_to is not None:
                raise ValueError("the last tier must have up_to null")
            bounds = [t.up_to for t in self.tiers[:-1]]
            if any(b is None for b in bounds) or bounds != sorted(set(bounds)):
                raise ValueError("tier up_to values must be set and strictly increasing")
        return self


class Pilot(BaseModel):
    fee: float = Field(ge=0)
    weeks: int = Field(ge=1, le=52)
    success_criteria: str


class PricingRecommendation(BaseModel):
    model: PricingModelName
    value_unit: str = Field(description="What the buyer pays for, e.g. 'kilometre surveyed'")
    rationale: str
    billing_period: Literal["MONTHLY", "QUARTERLY", "ANNUAL"] = "MONTHLY"
    currency: str = "usd"
    platform_fee: float | None = Field(default=None, description="Fixed fee per billing period (per seat for seat_plus_usage)")
    meter: Meter
    usage_price: UsagePrice
    pilot: Pilot | None = None
    aws_marketplace_dimension: str | None = None

    @model_validator(mode="after")
    def _fee(self):
        if self.model in ("platform_plus_usage", "seat_plus_usage") and not self.platform_fee:
            raise ValueError(f"{self.model} needs platform_fee")
        if self.platform_fee is not None and self.platform_fee < 0:
            raise ValueError("platform_fee must be >= 0")
        return self


class PricingResult(BaseModel):
    startup_id: str
    source: Literal["model", "model-retry", "rules", "rules-fallback", "mentor"]
    recommendation: PricingRecommendation
    errors: list[str] = Field(default_factory=list)
    raw_output: str | None = None
    approved: bool = False
    created_at: str = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------- prompt
# Training data (training.py) uses EXACTLY these strings, so what Qwen learns is what it sees.

PRICING_SYSTEM = (
    "You are the Maxelerator pricing analyst for deeptech startups. Read the startup profile and "
    "assign one pricing model. Reply with ONLY a JSON object with keys: model (usage | prepaid_credits | "
    "platform_plus_usage | seat_plus_usage | outcome_based), value_unit, rationale, billing_period "
    "(MONTHLY | QUARTERLY | ANNUAL), currency, platform_fee (number or null), meter {event_name, "
    "aggregation (COUNT | SUM | MAX | COUNT_UNIQUE | LATEST), field}, usage_price {billing_model "
    "(FLAT_FEE | PACKAGE | TIERED), unit_amount, package_size, tiers [{up_to, unit_amount}]}, "
    "pilot {fee, weeks, success_criteria} or null, aws_marketplace_dimension (string or null). "
    "Prices are hypotheses to validate with buyers. Never invent traction."
)

PROFILE_FIELDS = ("name", "sector", "stage", "product", "target_customers", "pricing_today", "traction", "sells_to_aws_customers")


def pricing_user(profile: StartupProfile | dict) -> str:
    data = profile.brief() if isinstance(profile, StartupProfile) else profile
    lines = [f"{k}: {data.get(k)}" for k in PROFILE_FIELDS if data.get(k) not in (None, "")]
    return "Startup profile\n" + "\n".join(lines)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (handles code fences and chatter)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON object in the reply")


def _errors(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        return [f"{'.'.join(str(p) for p in e['loc']) or 'root'}: {e['msg']}" for e in exc.errors()]
    return [str(exc)]


# ---------------------------------------------------------------------------- rules baseline

_HARDWARE = ("robot", "sensor", "drone", "device", "equipment", "hardware", "cobot", "magnetometer", "battery", "satellite", "instrument")
_COMPUTE = ("agent", "api", "model", "simulation", "design", "compute", "inference", "llm", "platform", "screening", "forecast")
_SEATS = ("per seat", "seat", "per user", "per site", "per engineer")
_OUTCOME = ("outcome", "savings", "saved", "yield", "per result", "success fee")


def _first_number(text: str) -> float | None:
    m = re.search(r"\$\s?([\d.,]+)\s?(k|m)?", text.lower())
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s or "usage")[:40]


def rules_recommend(profile: StartupProfile | dict) -> PricingRecommendation:
    """Deterministic baseline. Also the fallback when the model's answer fails validation."""
    d = profile.brief() if isinstance(profile, StartupProfile) else profile
    text = " ".join(str(d.get(k, "")) for k in ("one_liner", "product", "target_customers", "pricing_today", "sector")).lower()
    early = d.get("stage") in ("idea", "pre-seed", "seed")
    has_pilot = "pilot" in text
    aws = bool(d.get("sells_to_aws_customers"))

    m = re.search(r"\bper ([a-z][a-z ]{2,30}?)(?=[;,.()]|$| in | for | per | and )", text)
    unit = m.group(1).strip() if m else None
    today = str(d.get("pricing_today", "")).lower()
    known_unit_price = None  # "$40k for 25 km" -> 1600 per km
    m2 = re.search(r"\$\s?([\d.,]+)\s?(k|m)?\b[^$]*?\bfor (\d+)\b", today)
    if m2:
        known_unit_price = _first_number(m2.group(0)) / int(m2.group(3))
    pilot = None
    if has_pilot or (early and any(k in text for k in _HARDWARE)):
        fee = (_first_number(today) if "pilot" in today else None) or 25000
        pilot = Pilot(fee=fee, weeks=12, success_criteria="Agreed baseline and target metric, measured over the pilot period")

    if any(k in text for k in _OUTCOME):
        unit = unit or "verified outcome"
        rec = dict(model="outcome_based", platform_fee=None,
                   meter=Meter(event_name=_slug(unit) + "_delivered", aggregation="COUNT"),
                   usage_price=UsagePrice(billing_model="FLAT_FEE", unit_amount="500"),
                   why="Buyers pay when the result is delivered, which lowers the risk of adopting a new technology.")
    elif any(k in text for k in _SEATS):
        unit = unit or "site"
        rec = dict(model="seat_plus_usage", platform_fee=1500,
                   meter=Meter(event_name=_slug(unit) + "_active", aggregation="COUNT_UNIQUE", field=f"{_slug(unit)}_id"),
                   usage_price=UsagePrice(billing_model="FLAT_FEE", unit_amount="0"),
                   why="Value scales with the number of sites or users covered; a per-seat fee is easy for buyers to budget.")
    elif any(k in text for k in _HARDWARE):
        unit = unit or "device-month"
        rec = dict(model="platform_plus_usage", platform_fee=2000,
                   meter=Meter(event_name=_slug(unit), aggregation="SUM", field="quantity"),
                   usage_price=UsagePrice(billing_model="TIERED", tiers=[
                       Tier(up_to=100, unit_amount=f"{known_unit_price or 40:.2f}"),
                       Tier(up_to=None, unit_amount=f"{(known_unit_price or 40) * 0.8:.2f}")]),
                   why="A platform fee covers fixed hardware and support costs; usage pricing tracks the value delivered in the field.")
    elif any(k in text for k in _COMPUTE):
        if early:
            unit = "credit"
            rec = dict(model="prepaid_credits", platform_fee=None,
                       meter=Meter(event_name="credits_used", aggregation="SUM", field="credits"),
                       usage_price=UsagePrice(billing_model="PACKAGE", unit_amount="100", package_size=100),
                       why="Prepaid credits let small teams start without a contract while compute costs stay covered.")
        else:
            unit = unit or "run"
            rec = dict(model="usage", platform_fee=None,
                       meter=Meter(event_name=_slug(unit) + "_completed", aggregation="COUNT"),
                       usage_price=UsagePrice(billing_model="TIERED", tiers=[Tier(up_to=1000, unit_amount="2.00"), Tier(up_to=None, unit_amount="1.50")]),
                       why="Each run carries real compute cost, so pricing per run keeps margin as customers scale.")
    else:
        unit = unit or "unit"
        rec = dict(model="platform_plus_usage", platform_fee=1000,
                   meter=Meter(event_name=_slug(unit), aggregation="SUM", field="quantity"),
                   usage_price=UsagePrice(billing_model="FLAT_FEE", unit_amount="10"),
                   why="A platform fee plus usage is a safe default until the unit of value is clearer.")

    why = rec.pop("why")
    return PricingRecommendation(
        value_unit=unit,
        rationale=f"{why} Prices are starting hypotheses; validate them in discovery calls.",
        billing_period="ANNUAL" if not early else "MONTHLY",
        pilot=pilot,
        aws_marketplace_dimension=rec["meter"].event_name if aws else None,
        **rec,
    )


# ---------------------------------------------------------------------------- recommend


def recommend(profile: StartupProfile, llm=None, max_tokens: int = 700) -> PricingResult:
    """Ask the model (local Qwen or any LLM); validate; retry once with the errors; else rules."""
    if llm is None:
        return PricingResult(startup_id=profile.startup_id, source="rules", recommendation=rules_recommend(profile))

    user = pricing_user(profile)
    errors: list[str] = []
    raw = None
    for attempt in (1, 2):
        prompt = user if attempt == 1 else (
            f"{user}\n\nYour previous answer was invalid: {'; '.join(errors[-5:])}. Reply with only the corrected JSON object."
        )
        try:
            raw = llm.complete(PRICING_SYSTEM, prompt, max_tokens=max_tokens).text
            rec = PricingRecommendation.model_validate(extract_json(raw))
            return PricingResult(startup_id=profile.startup_id, source="model" if attempt == 1 else "model-retry",
                                 recommendation=rec, errors=errors, raw_output=raw)
        except Exception as exc:  # invalid JSON, schema errors, or the model server being down
            errors += [f"attempt {attempt}: {e}" for e in _errors(exc)]
    return PricingResult(startup_id=profile.startup_id, source="rules-fallback", recommendation=rules_recommend(profile),
                         errors=errors, raw_output=raw)


# ---------------------------------------------------------------------------- FlexPrice plan


def flexprice_steps(rec: PricingRecommendation, product_name: str, lookup_prefix: str) -> list[dict]:
    """The API calls that create this pricing model in FlexPrice. `$name.field` refers to an earlier step's response."""
    meter = {
        "name": f"{rec.value_unit} meter",
        "event_name": rec.meter.event_name,
        "aggregation": {"type": rec.meter.aggregation, **({"field": rec.meter.field} if rec.meter.field else {})},
        "reset_usage": "BILLING_PERIOD",
    }
    period = {"currency": rec.currency, "billing_period": rec.billing_period, "billing_period_count": 1}
    steps = [
        {"name": "feature", "method": "POST", "path": "/features", "body": {
            "name": rec.value_unit.capitalize(), "type": "metered", "lookup_key": rec.meter.event_name,
            "unit_singular": rec.value_unit, "meter": meter}},
        {"name": "plan", "method": "POST", "path": "/plans", "body": {
            "name": f"{product_name} {rec.model.replace('_', ' ').title()}",
            "lookup_key": f"{lookup_prefix}-{rec.model}".replace("_", "-"),
            "description": rec.rationale[:500]}},
    ]
    if rec.platform_fee:
        steps.append({"name": "platform_fee", "method": "POST", "path": "/prices", "body": {
            "entity_type": "PLAN", "entity_id": "$plan.id", "type": "FIXED", "billing_model": "FLAT_FEE",
            "amount": str(rec.platform_fee), "invoice_cadence": "ADVANCE", **period,
            "description": "Platform fee" + (" per seat" if rec.model == "seat_plus_usage" else "")}})
    up = rec.usage_price
    usage = {"entity_type": "PLAN", "entity_id": "$plan.id", "type": "USAGE", "meter_id": "$feature.meter_id",
             "billing_model": up.billing_model, "invoice_cadence": "ARREAR", **period}
    if up.billing_model == "FLAT_FEE":
        usage["amount"] = up.unit_amount
    elif up.billing_model == "PACKAGE":
        usage |= {"amount": up.unit_amount, "transform_quantity": {"divide_by": up.package_size, "round": "up"}}
    else:
        usage |= {"tier_mode": "SLAB", "tiers": [t.model_dump() for t in up.tiers]}
    steps.append({"name": "usage_price", "method": "POST", "path": "/prices", "body": usage})
    if rec.pilot:
        steps += [
            {"name": "pilot_plan", "method": "POST", "path": "/plans", "body": {
                "name": f"{product_name} Paid Pilot ({rec.pilot.weeks} weeks)",
                "lookup_key": f"{lookup_prefix}-pilot", "description": rec.pilot.success_criteria[:500]}},
            {"name": "pilot_fee", "method": "POST", "path": "/prices", "body": {
                "entity_type": "PLAN", "entity_id": "$pilot_plan.id", "type": "FIXED", "billing_model": "FLAT_FEE",
                "amount": str(rec.pilot.fee), "currency": rec.currency, "billing_period": "ONETIME",
                "billing_period_count": 1, "invoice_cadence": "ADVANCE", "description": "Paid pilot"}},
        ]
    return steps


def _resolve(value, results: dict):
    if isinstance(value, str) and value.startswith("$"):
        step, _, field = value[1:].partition(".")
        data = results[step]
        found = data.get(field)
        if found is None and field == "meter_id":  # some responses nest the meter
            found = (data.get("meter") or {}).get("id")
        if found is None:
            raise KeyError(f"step '{step}' response has no '{field}'")
        return found
    if isinstance(value, dict):
        return {k: _resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, results) for v in value]
    return value


def apply_steps(client, steps: list[dict]) -> dict:
    """Run the steps against a FlexPriceClient. Returns each step's response by name."""
    results: dict = {}
    for step in steps:
        body = _resolve(step["body"], results)
        results[step["name"]] = client._request(step["method"], step["path"], body)[1]
    return results


def setup_markdown(rec: PricingRecommendation) -> str:
    up = rec.usage_price
    if up.billing_model == "TIERED":
        price = ", ".join(f"{'up to ' + str(t.up_to) if t.up_to else 'above'}: ${t.unit_amount}" for t in up.tiers)
    elif up.billing_model == "PACKAGE":
        price = f"${up.unit_amount} per {up.package_size} {rec.value_unit}s"
    else:
        price = f"${up.unit_amount} per {rec.value_unit}"
    rows = [
        ("Pricing model", rec.model.replace("_", " ")),
        ("Value unit", rec.value_unit),
        ("Metered feature", f"`{rec.meter.event_name}`, {rec.meter.aggregation}" + (f" on `{rec.meter.field}`" if rec.meter.field else "")),
        ("Platform fee", f"${rec.platform_fee:,.0f} per {rec.billing_period.lower()} period" if rec.platform_fee else "none"),
        ("Usage price", f"{up.billing_model}: {price}"),
        ("Paid pilot", f"${rec.pilot.fee:,.0f}, {rec.pilot.weeks} weeks" if rec.pilot else "none"),
        ("AWS Marketplace dimension", rec.aws_marketplace_dimension or "not listed"),
    ]
    example = {"event_name": rec.meter.event_name, "external_customer_id": "<your customer id>",
               "properties": {rec.meter.field: "1"} if rec.meter.field else {}}
    table = "| Item | Setting |\n| --- | --- |\n" + "\n".join(f"| {a} | {b} |" for a, b in rows)
    return f"{table}\n\n{rec.rationale}\n\nExample usage event:\n\n```json\n{json.dumps(example, indent=2)}\n```"
