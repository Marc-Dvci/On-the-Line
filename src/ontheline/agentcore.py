"""Stateless Amazon Bedrock AgentCore extraction runtime for On the Line."""

from __future__ import annotations

import logging

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ontheline.config import Settings
from ontheline.domain import AgentCoreInvocationRequest
from ontheline.workflow import run_strands_extraction

LOGGER = logging.getLogger("ontheline.agentcore")
MAX_INVOCATION_BYTES = 500_000


async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "Healthy"})


async def invoke(request: Request) -> JSONResponse:
    try:
        body = await request.body()
        if len(body) > MAX_INVOCATION_BYTES:
            return JSONResponse({"error": "request body is too large"}, status_code=413)
        payload = AgentCoreInvocationRequest.model_validate_json(body)
    except ValidationError as error:
        return JSONResponse(
            {"error": "request validation failed", "details": error.errors(include_url=False)},
            status_code=422,
        )
    except ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    item = payload.input
    task = {
        "id": item.session_id,
        "organization_name": item.organization_name,
        "service_name": item.service_name,
        "fields": item.fields,
    }
    trace: list[dict[str, str]] = []
    try:
        extraction = await run_in_threadpool(
            run_strands_extraction,
            request.app.state.settings,
            task,
            item.transcript,
            trace,
        )
    except Exception as error:
        LOGGER.exception("AgentCore extraction failed", exc_info=error)
        return JSONResponse({"error": "extraction failed"}, status_code=502)
    return JSONResponse({
        "output": {"extraction": extraction.model_dump(mode="json"), "trace": trace}
    })


def create_app(settings: Settings | None = None) -> Starlette:
    runtime_settings = settings or Settings()
    app = Starlette(routes=[
        Route("/ping", ping),
        Route("/invocations", invoke, methods=["POST"]),
    ])
    app.state.settings = runtime_settings
    return app


app = create_app()
