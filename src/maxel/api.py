"""HTTP API for the Maxelerator program team (Starlette).

    GET  /health
    POST /startups                          onboard (profile JSON; optional "credits")
    GET  /startups                          list
    GET  /startups/{id}                     profile + billing link
    GET  /startups/{id}/credits             wallet balance and playbook cost
    POST /startups/{id}/credits/top-up      {"credits": 200}
    POST /startups/{id}/playbooks           {"mode": "local"} runs now; {"mode": "band"} returns the kickoff message
    GET  /startups/{id}/playbooks           list playbooks
    GET  /startups/{id}/playbooks/latest    meta + Markdown (?format=md for raw Markdown)

Auth: every route except /health needs `Authorization: Bearer $MAXEL_ADMIN_TOKEN` when the
token is set. Run: `maxel serve` or `uvicorn maxel.api:app`.
"""

from __future__ import annotations

import hmac
import json
import logging

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from .band_runtime import kickoff_message
from .llm import llm_from_settings
from .local_runner import LocalStudio
from .models import StartupProfile
from .provisioning import onboard_startup, top_up
from .store import NotFound
from .studio import Studio, build_studio
from .tools import InsufficientCredits

log = logging.getLogger("maxel.api")


def create_app(studio: Studio | None = None) -> Starlette:
    studio = studio or build_studio()

    def authorized(request: Request) -> bool:
        token = studio.settings.admin_token
        if not token:
            return True
        header = request.headers.get("authorization", "")
        return hmac.compare_digest(header, f"Bearer {token}")

    def guard(handler):
        async def wrapped(request: Request):
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await handler(request)
            except NotFound as exc:
                return JSONResponse({"error": str(exc).strip("'\"")}, status_code=404)
            except ValidationError as exc:
                return JSONResponse({"error": "invalid input", "details": json.loads(exc.json())}, status_code=422)
            except InsufficientCredits as exc:
                return JSONResponse({"error": str(exc)}, status_code=402)
            except (ValueError, json.JSONDecodeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

        return wrapped

    def public(profile: StartupProfile) -> dict:
        return profile.model_dump(exclude={"founder_email"})

    async def health(_: Request):
        return JSONResponse({"ok": True, "meter": studio.settings.meter, "store": studio.settings.store})

    async def create_startup(request: Request):
        body = await request.json()
        credits = float(body.pop("credits", studio.config.default_credits_per_startup))
        profile = StartupProfile(**body)
        profile = await run_in_threadpool(
            onboard_startup, profile, studio.store, studio.meter, credits, studio.settings.wallet_currency
        )
        return JSONResponse(public(profile), status_code=201)

    async def list_startups(_: Request):
        return JSONResponse([public(p) for p in studio.store.list_startups()])

    async def get_startup(request: Request):
        return JSONResponse(public(studio.store.get_startup(request.path_params["sid"])))

    async def credits(request: Request):
        data = await run_in_threadpool(studio.tools.check_credits, request.path_params["sid"])
        return JSONResponse(json.loads(data))

    async def credits_top_up(request: Request):
        body = await request.json()
        amount = float(body["credits"])
        if amount <= 0:
            raise ValueError("credits must be positive")
        await run_in_threadpool(top_up, request.path_params["sid"], amount, studio.store, studio.meter)
        data = await run_in_threadpool(studio.tools.check_credits, request.path_params["sid"])
        return JSONResponse(json.loads(data))

    async def create_playbook(request: Request):
        sid = request.path_params["sid"]
        body = await request.json() if await request.body() else {}
        mode = body.get("mode", "local")
        studio.store.get_startup(sid)  # 404 early
        if mode == "band":
            return JSONResponse(
                {
                    "mode": "band",
                    "next_step": "Post this message in a BAND room that contains the five studio agents.",
                    "kickoff_message": kickoff_message(studio, sid),
                },
                status_code=202,
            )
        if mode != "local":
            raise ValueError("mode must be 'local' or 'band'")
        playbook_id = await run_in_threadpool(studio.tools.start_playbook, sid, "local")
        runner = LocalStudio(studio, llm_from_settings(studio.settings))
        task = BackgroundTask(_run_safely, runner, sid, playbook_id)
        return JSONResponse(
            {"mode": "local", "playbook_id": playbook_id, "status": "in_progress", "poll": f"/startups/{sid}/playbooks/latest"},
            status_code=202,
            background=task,
        )

    async def list_playbooks(request: Request):
        metas = studio.store.list_playbooks(request.path_params["sid"])
        return JSONResponse([m.model_dump() for m in metas])

    async def latest_playbook(request: Request):
        sid = request.path_params["sid"]
        meta = studio.store.latest_playbook(sid)
        markdown = studio.store.get_playbook_markdown(sid, meta.playbook_id)
        if request.query_params.get("format") == "md":
            if markdown is None:
                return JSONResponse({"error": "playbook not finished", "status": meta.status}, status_code=409)
            return PlainTextResponse(markdown, media_type="text/markdown")
        return JSONResponse({"meta": meta.model_dump(), "markdown": markdown})

    routes = [
        Route("/health", health),
        Route("/startups", guard(create_startup), methods=["POST"]),
        Route("/startups", guard(list_startups), methods=["GET"]),
        Route("/startups/{sid}", guard(get_startup), methods=["GET"]),
        Route("/startups/{sid}/credits", guard(credits), methods=["GET"]),
        Route("/startups/{sid}/credits/top-up", guard(credits_top_up), methods=["POST"]),
        Route("/startups/{sid}/playbooks", guard(create_playbook), methods=["POST"]),
        Route("/startups/{sid}/playbooks", guard(list_playbooks), methods=["GET"]),
        Route("/startups/{sid}/playbooks/latest", guard(latest_playbook), methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.studio = studio
    return app


def _run_safely(runner: LocalStudio, startup_id: str, playbook_id: str) -> None:
    try:
        runner.run(startup_id, playbook_id)
    except Exception:  # recorded on the playbook meta by the runner
        log.exception("playbook %s failed", playbook_id)


def __getattr__(name: str):
    # `uvicorn maxel.api:app` builds the app lazily from environment settings.
    if name == "app":
        return create_app()
    raise AttributeError(name)
