import json

import pytest
import requests

from maxel.metering import FlexPriceClient, FlexPriceError, FlexPriceMeter
from maxel.models import Billing, StartupProfile


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def client(responses):
    session = FakeSession(responses)
    return FlexPriceClient("fp_test", "https://us.api.flexprice.io/v1", session=session, backoff=0), session


def test_ingest_event_shape():
    c, s = client([FakeResponse(202)])
    c.ingest(c.event("maxel.credits", "maxel-qubit-sense", {"credits": 10, "action": "section"}, event_id="pb-1:market_icp"))
    method, url, body = s.calls[0]
    assert (method, url) == ("POST", "https://us.api.flexprice.io/v1/events")
    assert body["event_name"] == "maxel.credits"
    assert body["external_customer_id"] == "maxel-qubit-sense"
    assert body["event_id"] == "pb-1:market_icp"
    assert body["properties"] == {"credits": "10", "action": "section"}
    assert s.headers["x-api-key"] == "fp_test"


def test_create_customer_409_is_success():
    c, _ = client([FakeResponse(409, {"message": "already exists"})])
    assert c.create_customer("maxel-x", "X")["already_exists"] is True


def test_retries_5xx_then_succeeds():
    c, s = client([FakeResponse(503), requests.ConnectionError("boom"), FakeResponse(202)])
    c.ingest(c.event("maxel.credits", "c", {"credits": 1}))
    assert len(s.calls) == 3


def test_4xx_is_not_retried():
    c, s = client([FakeResponse(400, {"error": "Missing required field: event_name"})])
    with pytest.raises(FlexPriceError) as exc:
        c.ingest({"external_customer_id": "c"})
    assert exc.value.status == 400 and len(s.calls) == 1


def test_wallet_create_and_balance():
    c, s = client([FakeResponse(200, {"id": "wal_1"}), FakeResponse(200, {"real_time_credit_balance": "412.5"})])
    assert c.create_wallet("maxel-x", 500)["id"] == "wal_1"
    body = s.calls[0][2]
    assert body["initial_credits_to_load"] == "500" and body["external_customer_id"] == "maxel-x"
    assert body["currency"] == "usd" and body["wallet_type"] == "PRE_PAID"
    assert c.wallet_balance("wal_1") == 412.5


def test_bulk_chunks_at_1000():
    c, s = client([FakeResponse(202), FakeResponse(202)])
    c.ingest_bulk([c.event("e", "c", {"credits": 1}) for _ in range(1500)])
    assert [len(call[2]["events"]) for call in s.calls] == [1000, 500]


def test_flexprice_meter_is_idempotent(tmp_path):
    c, s = client([FakeResponse(202)])
    meter = FlexPriceMeter(tmp_path / "ledger.jsonl", c)
    startup = StartupProfile(
        startup_id="x-co", name="X", one_liner="x", sector="robotics", product="p", target_customers="t",
        billing=Billing(external_id="maxel-x-co", initial_credits=100),
    )
    assert meter.charge(startup, 10, "section", "pb:sec") is True
    assert meter.charge(startup, 10, "section", "pb:sec") is False  # no second HTTP call
    assert len(s.calls) == 1
    assert s.calls[0][2]["external_customer_id"] == "maxel-x-co"


def test_onboard_and_run_against_flexprice(studio, profile):
    """Full flow with FlexPrice metering: customer + wallet at onboarding, 10 events per playbook."""
    from maxel.llm import StubLLM
    from maxel.local_runner import LocalStudio
    from maxel.provisioning import onboard_startup

    responses = [FakeResponse(201, {"id": "cust_1"}), FakeResponse(200, {"id": "wal_1"}),
                 FakeResponse(200, {"real_time_credit_balance": "500"})]  # balance check at start
    responses += [FakeResponse(202)] * 10 + [FakeResponse(200, {"real_time_credit_balance": "410"})]
    c, s = client(responses)
    studio.meter = studio.tools.meter = FlexPriceMeter(studio.settings.data_dir / "ledger.jsonl", c)

    onboarded = onboard_startup(profile, studio.store, studio.meter, credits=500)
    assert onboarded.billing.wallet_id == "wal_1" and onboarded.billing.flexprice_customer_id == "cust_1"
    LocalStudio(studio, StubLLM()).run("qubit-sense")

    paths = [url.rsplit("/v1", 1)[1] for _, url, _ in s.calls]
    assert paths[:3] == ["/customers", "/wallets", "/wallets/wal_1/balance/real-time"]
    events = [body for (_, url, body) in s.calls if url.endswith("/events")]
    assert len(events) == 10
    assert sum(float(e["properties"]["credits"]) for e in events) == 95
    assert {e["external_customer_id"] for e in events} == {"maxel-qubit-sense"}
    assert len({e["event_id"] for e in events}) == 10
    assert events[0]["properties"]["action"] == "pricing"
