"""Onboarding: register a startup and give it a FlexPrice customer and a prepaid credit wallet."""

from __future__ import annotations

import logging

from .metering import FlexPriceMeter, Meter
from .models import Billing, StartupProfile
from .store import BaseStore, NotFound

log = logging.getLogger("maxel.provisioning")


def external_id_for(startup_id: str) -> str:
    """The FlexPrice External ID. Usage events must carry exactly this value (case-sensitive)."""
    return f"maxel-{startup_id}"


def onboard_startup(profile: StartupProfile, store: BaseStore, meter: Meter, credits: float, currency: str = "usd") -> StartupProfile:
    """Idempotent: re-running keeps the existing wallet and billing link."""
    try:
        existing = store.get_startup(profile.startup_id)
    except NotFound:
        existing = None

    if existing and existing.billing:
        profile.billing = existing.billing
        profile.created_at = existing.created_at
        store.put_startup(profile)
        log.info("updated profile for %s (billing unchanged)", profile.startup_id)
        return profile

    billing = Billing(external_id=external_id_for(profile.startup_id), initial_credits=credits)

    if isinstance(meter, FlexPriceMeter):
        client = meter.client
        customer = client.create_customer(
            billing.external_id,
            profile.name,
            email=profile.founder_email,
            metadata={"program": "maxelerator", "sector": profile.sector, "stage": profile.stage},
        )
        billing.flexprice_customer_id = customer.get("id")
        wallet = client.create_wallet(
            billing.external_id, credits, currency=currency, customer_id=billing.flexprice_customer_id
        )
        billing.wallet_id = wallet.get("id")
        log.info("FlexPrice customer %s, wallet %s, %s credits", billing.external_id, billing.wallet_id, credits)

    profile.billing = billing
    store.put_startup(profile)
    return profile


def top_up(startup_id: str, credits: float, store: BaseStore, meter: Meter) -> StartupProfile:
    startup = store.get_startup(startup_id)
    if startup.billing is None:
        raise ValueError(f"{startup_id} has no billing record; onboard it first")
    if isinstance(meter, FlexPriceMeter) and startup.billing.wallet_id:
        meter.client.top_up(startup.billing.wallet_id, credits)
    startup.billing.initial_credits += credits  # keeps the offline ledger balance in step
    store.put_startup(startup)
    return startup
