"""API tests with a tiny ASGI driver (no httpx needed)."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from maxel.api import create_app
from maxel.studio import build_studio

ROOT = Path(__file__).resolve().parents[1]


def call(app, method, path, body=None, token=None, query=""):
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": headers,
        "scheme": "http", "server": ("test", 80), "client": ("test", 1), "root_path": "",
    }
    sent = {"done": False}
    out = {"status": None, "body": b""}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    text = out["body"].decode()
    try:
        return out["status"], json.loads(text)
    except ValueError:
        return out["status"], text


def make_app(studio, token=None):
    s = replace(studio.settings, admin_token=token)
    return create_app(build_studio(s))


def test_full_api_flow(studio):
    app = make_app(studio)
    profile = json.loads((ROOT / "examples/startups/foldwise-bio.json").read_text())
    status, data = call(app, "POST", "/startups", {**profile, "credits": 200})
    assert status == 201 and data["billing"]["external_id"] == "maxel-foldwise-bio"
    assert "founder_email" not in data

    status, data = call(app, "GET", "/startups/foldwise-bio/credits")
    assert status == 200 and data["credits_available"] == 200 and data["can_start"] is True

    # local run executes as a background task after the 202 response
    status, data = call(app, "POST", "/startups/foldwise-bio/playbooks", {"mode": "local"})
    assert status == 202 and data["playbook_id"].startswith("pb-")

    status, data = call(app, "GET", "/startups/foldwise-bio/playbooks/latest")
    assert status == 200 and data["meta"]["status"] == "complete"
    assert "# Foldwise Bio: Go-to-Market Playbook" in data["markdown"]

    status, md = call(app, "GET", "/startups/foldwise-bio/playbooks/latest", query="format=md")
    assert status == 200 and md.startswith("# Foldwise Bio")

    status, data = call(app, "POST", "/startups/foldwise-bio/playbooks", {"mode": "band"})
    assert status == 202 and "@Maxel Strategist" in data["kickoff_message"]

    status, data = call(app, "POST", "/startups/foldwise-bio/credits/top-up", {"credits": 50})
    assert data["credits_available"] == 155  # 200 - 95 + 50


def test_errors_and_auth(studio):
    app = make_app(studio, token="s3cret")
    assert call(app, "GET", "/health")[0] == 200
    assert call(app, "GET", "/startups")[0] == 401
    assert call(app, "GET", "/startups/nope", token="s3cret")[0] == 404
    status, data = call(app, "POST", "/startups", {"startup_id": "Bad Id", "name": "x"}, token="s3cret")
    assert status == 422

    profile = json.loads((ROOT / "examples/startups/qubit-sense.json").read_text())
    call(app, "POST", "/startups", {**profile, "credits": 20}, token="s3cret")
    status, data = call(app, "POST", "/startups/qubit-sense/playbooks", {"mode": "local"}, token="s3cret")
    assert status == 402 and "credits" in data["error"]
