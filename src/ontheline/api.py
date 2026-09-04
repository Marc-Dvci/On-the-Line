"""Production web API and curator console entry point."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from ontheline.config import Settings, verify_password
from ontheline.database import Store
from ontheline.domain import (
    ConsentRequest,
    DecisionRequest,
    DemoRequest,
    FailureReportRequest,
    HSDSImportRequest,
    IntakeSearch,
    LoginRequest,
    ReferralAcceptRequest,
    VerificationRequest,
)
from ontheline.privacy import redact_pii
from ontheline.telephony import (
    TelephonyError,
    intake_twiml,
    place_outbound_call,
    twilio_intake_media,
    twilio_media,
    twilio_status,
    twiml,
)
from ontheline.workflow import VerificationWorkflow, demo_transcript

COOKIE_NAME = "ontheline_session"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class WindowLimiter:
    def __init__(self, maximum: int, window_seconds: int = 60):
        self.maximum = maximum
        self.window_seconds = window_seconds
        self.attempts: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        if len(self.attempts) > 10_000:
            cutoff = now - self.window_seconds
            stale = [name for name, values in self.attempts.items() if not values or values[-1] < cutoff]
            for name in stale:
                self.attempts.pop(name, None)
        bucket = self.attempts[key]
        while bucket and bucket[0] < now - self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.maximum:
            return False
        bucket.append(now)
        return True


class RequestBodyLimitMiddleware:
    """Bound request bodies even when clients omit Content-Length."""

    def __init__(self, app, maximum: int = 10_000_000):
        self.app = app
        self.maximum = maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.maximum + 1
        if declared > self.maximum:
            await json_error("request body is too large", 413)(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.maximum:
                raise HTTPException(413, "request body is too large")
            return message

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app, https_only: bool = False):
        self.app = app
        self.https_only = https_only

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend([
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                ])
                if scope.get("path", "").startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                if self.https_only:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
            await send(message)
        await self.app(scope, receive, send_with_headers)


def json_error(message: str, status: int, *, code: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code or f"http_{status}"}}, status_code=status)


async def parse_model(request: Request, model, *, max_bytes: int = 256_000):
    try:
        content_length = int(request.headers.get("content-length", "0") or 0)
        if content_length > max_bytes:
            raise HTTPException(413, "request body is too large")
        body = await request.body()
        if len(body) > max_bytes:
            raise HTTPException(413, "request body is too large")
        return model.model_validate_json(body)
    except ValidationError as error:
        raise HTTPException(422, error.errors(include_url=False)) from error
    except ValueError as error:
        raise HTTPException(400, "invalid JSON body") from error


def token_hash(token: str, secret_key: str) -> str:
    return hmac.new(secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def current_user(request: Request, *, role: str | None = None) -> dict[str, Any]:
    token = request.cookies.get(COOKIE_NAME, "")
    secret_key = request.app.state.settings.secret_key
    user = request.app.state.store.session_user(token_hash(token, secret_key)) if token else None
    if not user:
        raise HTTPException(401, "authentication required")
    if role == "curator" and user["role"] != "curator":
        raise HTTPException(403, "curator access required")
    if request.method in MUTATING_METHODS:
        supplied = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(supplied, user["csrf_token"]):
            raise HTTPException(403, "invalid CSRF token")
    return user


async def health(request: Request) -> JSONResponse:
    try:
        with request.app.state.store.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return JSONResponse({"status": "ok", "service": "ontheline", "version": "1.0.0"})
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)


async def ping(request: Request) -> JSONResponse:
    """Amazon Bedrock AgentCore Runtime health contract."""
    return await health(request)


async def hsds_root(request: Request) -> JSONResponse:
    base = request.app.state.settings.public_base_url.rstrip("/")
    return JSONResponse({
        "version": "3.2",
        "profile": f"{base}/hsds/profile",
        "openapi_url": "https://raw.githubusercontent.com/openreferral/specification/3.2/schema/openapi.json",
    })


async def hsds_profile(request: Request) -> JSONResponse:
    return JSONResponse({
        "name": "On the Line HSDS 3.2 profile",
        "description": "Dereferenced community service records with On the Line assurance metadata.",
        "schema_version": "3.2",
    })


async def hsds_services(request: Request) -> JSONResponse:
    records = request.app.state.store.hsds_services()
    return JSONResponse({"contents": records, "total_items": len(records)})


async def hsds_service(request: Request) -> JSONResponse:
    records = request.app.state.store.hsds_services(request.path_params["service_id"])
    if not records:
        raise HTTPException(404, "service not found")
    return JSONResponse(records[0])


async def login(request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.login_limiter.allow(ip):
        return json_error("too many login attempts; try again shortly", 429, code="rate_limited")
    payload = await parse_model(request, LoginRequest)
    user = request.app.state.store.user_by_username(payload.username)
    valid = user and await run_in_threadpool(
        verify_password, payload.password, user["password_hash"]
    )
    if not valid:
        return json_error("invalid username or password", 401, code="invalid_credentials")
    raw_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    settings = request.app.state.settings
    request.app.state.store.create_session(
        user["id"], token_hash(raw_token, settings.secret_key), csrf, settings.session_hours
    )
    response = JSONResponse({"user": {"id": user["id"], "username": user["username"],
                                           "display_name": user["display_name"], "role": user["role"]},
                                 "csrf_token": csrf})
    response.set_cookie(COOKIE_NAME, raw_token, max_age=settings.session_hours * 3600,
                        httponly=True, secure=settings.https_only, samesite="strict", path="/")
    return response


async def logout(request: Request) -> Response:
    current_user(request)
    raw = request.cookies.get(COOKIE_NAME, "")
    settings = request.app.state.settings
    request.app.state.store.delete_session(token_hash(raw, settings.secret_key))
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


async def me(request: Request) -> JSONResponse:
    user = current_user(request)
    return JSONResponse({"user": {key: user[key] for key in ("id", "username", "display_name", "role")},
                         "csrf_token": user["csrf_token"]})


async def dashboard(request: Request) -> JSONResponse:
    current_user(request)
    return JSONResponse(request.app.state.store.dashboard())


async def services(request: Request) -> JSONResponse:
    current_user(request)
    query = request.query_params.get("q", "")[:120]
    return JSONResponse({"services": request.app.state.store.list_services(query)})


async def import_directory(request: Request) -> JSONResponse:
    user = current_user(request, role="curator")
    payload = await parse_model(request, HSDSImportRequest, max_bytes=10_000_000)
    try:
        result = await run_in_threadpool(
            request.app.state.store.import_hsds_services, payload.services, user["id"]
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(result, status_code=201)


async def create_consent(request: Request) -> JSONResponse:
    user = current_user(request, role="curator")
    payload = await parse_model(request, ConsentRequest)
    try:
        result = request.app.state.store.create_consent(payload, user["id"])
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return JSONResponse(result, status_code=201)


async def tasks(request: Request) -> JSONResponse:
    current_user(request)
    return JSONResponse({"tasks": request.app.state.store.list_tasks()})


async def refresh_queue(request: Request) -> JSONResponse:
    user = current_user(request, role="curator")
    return JSONResponse(request.app.state.store.refresh_queue(user["id"]))


async def decisions(request: Request) -> JSONResponse:
    current_user(request)
    return JSONResponse({"decisions": request.app.state.store.decisions()})


async def verify_task(request: Request) -> JSONResponse:
    current_user(request, role="curator")
    payload = await parse_model(request, VerificationRequest)
    if payload.task_id != request.path_params["task_id"]:
        raise HTTPException(400, "task id mismatch")
    try:
        result = await run_in_threadpool(
            request.app.state.workflow.run,
            payload.task_id,
            payload.transcript,
            payload.demo_scenario,
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(result, status_code=201)


async def run_demo(request: Request) -> JSONResponse:
    current_user(request, role="curator")
    if not request.app.state.settings.bootstrap_sample_data:
        raise HTTPException(404, "provider sandbox is not enabled")
    payload = await parse_model(request, DemoRequest)
    task_id = request.path_params["task_id"]
    try:
        result = await run_in_threadpool(
            request.app.state.workflow.run,
            task_id,
            demo_transcript(payload.scenario),
            payload.scenario,
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(result, status_code=201)


async def resolve_decision(request: Request) -> JSONResponse:
    user = current_user(request, role="curator")
    payload = await parse_model(request, DecisionRequest)
    try:
        result = request.app.state.store.resolve_decision(
            request.path_params["proposal_id"], payload.action, user["id"],
            payload.edited_value, payload.note
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(result)


async def audit(request: Request) -> JSONResponse:
    current_user(request)
    return JSONResponse({"events": request.app.state.store.audit_events(),
                         "chain_valid": request.app.state.store.verify_audit_chain()})


async def intake_search(request: Request) -> JSONResponse:
    payload = await parse_model(request, IntakeSearch)
    results = request.app.state.store.search_referrals(payload.need, payload.postal_code, payload.limit)
    return JSONResponse({"results": results, "disclaimer": "Availability can change. Call before travel."})


async def accept_referral(request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.public_limiter.allow(ip):
        return json_error("too many referrals; try again shortly", 429, code="rate_limited")
    payload = await parse_model(request, ReferralAcceptRequest)
    service = request.app.state.store.service(payload.service_id)
    if not service:
        raise HTTPException(404, "service not found")
    if service["status"] != "active":
        raise HTTPException(409, "service is not currently active")
    try:
        referral_id = request.app.state.store.record_referral(
            payload.service_id, redact_pii(payload.need), payload.postal_code
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse({"referral_id": referral_id}, status_code=201)


async def report_failure(request: Request) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.public_limiter.allow(ip):
        return json_error("too many reports; try again shortly", 429, code="rate_limited")
    payload = await parse_model(request, FailureReportRequest)
    try:
        report_id, task_id = request.app.state.store.report_failure(
            payload.service_id, payload.reason, redact_pii(payload.details), payload.referral_id
        )
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse({"report_id": report_id, "verification_task_id": task_id}, status_code=201)


async def call_task(request: Request) -> JSONResponse:
    current_user(request, role="curator")
    settings = request.app.state.settings
    if settings.telephony_provider != "twilio":
        raise HTTPException(409, "live telephony is not configured")
    task_id = request.path_params["task_id"]
    try:
        request.app.state.store.claim_task(task_id, "curator")
        result = await place_outbound_call(settings, request.app.state.store, task_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, TelephonyError) as error:
        raise HTTPException(409, str(error)) from error
    return JSONResponse(result, status_code=202)


async def system_info(request: Request) -> JSONResponse:
    current_user(request)
    settings = request.app.state.settings
    return JSONResponse({"name": "On the Line", "version": "1.0.0", "agent_mode": settings.agent_mode,
                         "aws_region": settings.aws_region,
                         "telephony_provider": settings.telephony_provider,
                         "provider_sandbox": settings.bootstrap_sample_data,
                         "audit_chain_valid": request.app.state.store.verify_audit_chain()})


async def index(request: Request) -> FileResponse:
    return FileResponse(request.app.state.web_dir / "index.html")


async def http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "request validation failed"
    return JSONResponse({"error": {"message": detail, "details": exc.detail if not isinstance(exc.detail, str) else None}},
                        status_code=exc.status_code)


async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    request.app.state.logger.exception("Unhandled request error", exc_info=exc)
    return json_error("an unexpected error occurred", 500)


def create_app(settings: Settings | None = None, store: Store | None = None) -> Starlette:
    import logging

    settings = settings or Settings()
    store = store or Store(settings.database_path)
    web_dir = Path(__file__).parent / "web"

    @asynccontextmanager
    async def lifespan(app: Starlette):
        store.initialize()
        store.seed_demo(settings)
        yield

    middleware = [
        Middleware(SecurityHeadersMiddleware, https_only=settings.https_only),
        Middleware(RequestBodyLimitMiddleware),
        Middleware(TrustedHostMiddleware, allowed_hosts=settings.host_list),
        Middleware(GZipMiddleware, minimum_size=800),
    ]
    if settings.https_only:
        middleware.append(Middleware(HTTPSRedirectMiddleware))
    routes = [
        Route("/health", health), Route("/ping", ping),
        Route("/hsds", hsds_root), Route("/hsds/profile", hsds_profile),
        Route("/hsds/services", hsds_services),
        Route("/hsds/services/{service_id:str}", hsds_service),
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]), Route("/api/auth/me", me),
        Route("/api/dashboard", dashboard), Route("/api/services", services), Route("/api/tasks", tasks),
        Route("/api/directory/import", import_directory, methods=["POST"]),
        Route("/api/consents", create_consent, methods=["POST"]),
        Route("/api/campaign/refresh", refresh_queue, methods=["POST"]),
        Route("/api/decisions", decisions), Route("/api/audit", audit), Route("/api/system", system_info),
        Route("/api/tasks/{task_id:str}/verify", verify_task, methods=["POST"]),
        Route("/api/tasks/{task_id:str}/demo", run_demo, methods=["POST"]),
        Route("/api/tasks/{task_id:str}/call", call_task, methods=["POST"]),
        Route("/api/decisions/{proposal_id:str}", resolve_decision, methods=["POST"]),
        Route("/api/intake/search", intake_search, methods=["POST"]),
        Route("/api/intake/referrals", accept_referral, methods=["POST"]),
        Route("/api/intake/failures", report_failure, methods=["POST"]),
        Route("/telephony/twiml/{task_id:str}", twiml, methods=["GET", "POST"]),
        Route("/telephony/intake/twiml", intake_twiml, methods=["POST"]),
        Route("/telephony/status/{call_id:str}", twilio_status, methods=["POST"]),
        WebSocketRoute("/telephony/twilio/{task_id:str}", twilio_media),
        WebSocketRoute("/telephony/intake/twilio", twilio_intake_media),
        Mount("/assets", app=StaticFiles(directory=web_dir), name="assets"),
        Route("/{path:path}", index),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan,
                    exception_handlers={HTTPException: http_exception, Exception: unhandled_exception})
    app.state.settings, app.state.store = settings, store
    app.state.workflow = VerificationWorkflow(store, settings)
    app.state.login_limiter = WindowLimiter(settings.login_attempts_per_minute)
    app.state.public_limiter = WindowLimiter(20)
    app.state.web_dir, app.state.logger = web_dir, logging.getLogger("ontheline.api")
    return app


app = create_app()
