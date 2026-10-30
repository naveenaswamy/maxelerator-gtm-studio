"""FlexPrice client and the studio's metering layer.

Every billable action (a playbook section saved, a playbook assembled) becomes ONE usage
event with a single numeric property, `credits`. In FlexPrice you create one metered
feature that SUMs `credits` for event_name `maxel.credits` (docs/FLEXPRICE_SETUP.md).
One dimension keeps billing simple and also fits AWS Marketplace, which meters a single
usage dimension per product.

API references (verified October 2026):
    POST /v1/events                      ingest one event (202 Accepted)
    POST /v1/events/bulk                 up to 1000 events
    POST /v1/customers                   external_id required; 409 = already exists
    POST /v1/wallets                     currency required; external_customer_id, initial_credits_to_load
    GET  /v1/wallets/{id}/balance/real-time
    POST /v1/wallets/{id}/top-up         credits_to_add
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("maxel.metering")


class FlexPriceError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"FlexPrice {path} -> HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class FlexPriceClient:
    """Thin, dependency-light client for the FlexPrice REST API."""

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://us.api.flexprice.io/v1",
        session: requests.Session | None = None,
        timeout: float = 10.0,
        max_attempts: int = 3,
        backoff: float = 0.5,
    ):
        if not api_key:
            raise ValueError("FLEXPRICE_API_KEY is required for FlexPrice metering")
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Content-Type": "application/json"})
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff = backoff

    def _request(self, method: str, path: str, body: dict | None = None, ok: tuple[int, ...] = (200, 201, 202)) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self.session.request(method, url, json=body, timeout=self.timeout)
            except requests.RequestException as exc:  # network error / timeout: retry
                last_exc = exc
            else:
                if resp.status_code in ok:
                    try:
                        return resp.status_code, resp.json() if resp.content else {}
                    except ValueError:
                        return resp.status_code, {}
                if resp.status_code not in self.RETRY_STATUSES:  # 4xx: fails the same way every time
                    raise FlexPriceError(resp.status_code, resp.text, path)
                last_exc = FlexPriceError(resp.status_code, resp.text, path)
            if attempt < self.max_attempts:
                time.sleep(self.backoff * 2 ** (attempt - 1))
        assert last_exc is not None
        raise last_exc

    # -- customers
    def create_customer(self, external_id: str, name: str, email: str | None = None, metadata: dict | None = None) -> dict:
        body = {"external_id": external_id, "name": name, "metadata": {k: str(v) for k, v in (metadata or {}).items()}}
        if email:
            body["email"] = email
        status, data = self._request("POST", "/customers", body, ok=(200, 201, 409))
        if status == 409:  # FlexPrice docs: treat 201 and 409 as success
            return {"external_id": external_id, "already_exists": True}
        return data

    # -- wallets (prepaid credits)
    def create_wallet(
        self,
        external_customer_id: str,
        credits: float,
        currency: str = "usd",
        conversion_rate: str = "1",
        name: str = "Maxelerator GTM credits",
        customer_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "currency": currency,
            "name": name,
            "conversion_rate": conversion_rate,
            "initial_credits_to_load": str(credits),
            "wallet_type": "PRE_PAID",
            "external_customer_id": external_customer_id,
        }
        if customer_id:
            body["customer_id"] = customer_id
        return self._request("POST", "/wallets", body)[1]

    def wallet_balance(self, wallet_id: str) -> float:
        data = self._request("GET", f"/wallets/{wallet_id}/balance/real-time")[1]
        for field in ("real_time_credit_balance", "credit_balance"):
            if data.get(field) not in (None, ""):
                return float(data[field])
        raise FlexPriceError(200, json.dumps(data), "wallet balance has no credit field")

    def top_up(self, wallet_id: str, credits: float, description: str = "Maxelerator credit top-up") -> dict:
        body = {"credits_to_add": str(credits), "description": description, "idempotency_key": uuid.uuid4().hex}
        return self._request("POST", f"/wallets/{wallet_id}/top-up", body)[1]

    # -- usage events
    @staticmethod
    def event(event_name: str, external_customer_id: str, properties: dict, event_id: str | None = None, timestamp: str | None = None) -> dict:
        return {
            "event_name": event_name,
            "external_customer_id": external_customer_id,
            "event_id": event_id or uuid.uuid4().hex,
            "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "maxel-gtm-studio",
            # The ingest schema types property values as strings; FlexPrice parses numbers for SUM.
            "properties": {k: str(v) for k, v in properties.items()},
        }

    def ingest(self, event: dict) -> None:
        self._request("POST", "/events", event)

    def ingest_bulk(self, events: list[dict]) -> None:
        for i in range(0, len(events), 1000):  # API limit: 1000 events per request
            self._request("POST", "/events/bulk", {"events": events[i : i + 1000]})


# --------------------------------------------------------------------------- meters


@dataclass
class Charge:
    startup_id: str
    credits: float
    action: str
    event_id: str
    properties: dict


class Meter:
    """Records charges in a local ledger. Subclasses also send them to a billing system.

    The ledger is the audit trail and the offline balance; event_id makes charges idempotent,
    so a retried tool call or a re-saved section is never charged twice.
    """

    def __init__(self, ledger_path: Path):
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text().splitlines() if line.strip()]

    def already_charged(self, event_id: str) -> bool:
        return any(row["event_id"] == event_id for row in self._ledger())

    def used(self, startup_id: str) -> float:
        return sum(row["credits"] for row in self._ledger() if row["startup_id"] == startup_id)

    def charge(self, startup, credits: float, action: str, event_id: str, **properties) -> bool:
        """Charge `credits` to a startup. Returns False when this event_id was already charged."""
        with self._lock:
            if self.already_charged(event_id):
                return False
            self._send(startup, credits, action, event_id, properties)
            row = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "startup_id": startup.startup_id,
                "credits": credits,
                "action": action,
                "event_id": event_id,
                **{k: str(v) for k, v in properties.items()},
            }
            with self.ledger_path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
            return True

    def balance(self, startup) -> float:
        initial = startup.billing.initial_credits if startup.billing else 0
        return initial - self.used(startup.startup_id)

    def _send(self, startup, credits, action, event_id, properties) -> None:  # noqa: D401
        """Hook for billing backends. The base meter is ledger-only."""


class LogMeter(Meter):
    """Offline/dev meter: ledger only. Balances are computed from the startup's initial credits."""


class FlexPriceMeter(Meter):
    def __init__(self, ledger_path: Path, client: FlexPriceClient, event_name: str = "maxel.credits"):
        super().__init__(ledger_path)
        self.client = client
        self.event_name = event_name

    def _send(self, startup, credits, action, event_id, properties) -> None:
        external_id = startup.billing.external_id if startup.billing else startup.startup_id
        self.client.ingest(
            self.client.event(
                self.event_name,
                external_id,
                {"credits": credits, "action": action, **properties},
                event_id=event_id,
            )
        )

    def balance(self, startup) -> float:
        """Real-time wallet balance from FlexPrice; falls back to the ledger estimate on error."""
        wallet_id = startup.billing.wallet_id if startup.billing else None
        if wallet_id:
            try:
                return self.client.wallet_balance(wallet_id)
            except (FlexPriceError, requests.RequestException) as exc:
                log.warning("wallet balance unavailable for %s, using ledger: %s", startup.startup_id, exc)
        return super().balance(startup)


def meter_from_settings(settings) -> Meter:
    ledger = Path(settings.data_dir) / "ledger.jsonl"
    if settings.meter == "flexprice":
        client = FlexPriceClient(settings.flexprice_api_key, settings.flexprice_base_url)
        return FlexPriceMeter(ledger, client, settings.flexprice_event_name)
    return LogMeter(ledger)
