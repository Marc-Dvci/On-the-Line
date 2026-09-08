"""Consent-gated Twilio calling and bidirectional media for the Nova Sonic agent."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode
from xml.sax.saxutils import escape

import httpx
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect

from ontheline.audio.telephony_io import TelephonyAudioInput, TelephonyAudioOutput
from ontheline.config import Settings
from ontheline.database import Store
from ontheline.domain import ExtractionResult, ProposedFieldChange, TranscriptTurn
from ontheline.policy import SAFETY_OUTCOMES, classify_extraction
from ontheline.privacy import redact_pii
from ontheline.readback import corroborate, quote_supported
from ontheline.spike.call_agent import CallRecord, build_agent
from ontheline.spike.intake_agent import IntakeRecord, build_intake_agent
from ontheline.workflow import extract_from_transcript

MAX_MEDIA_MESSAGE_BYTES = 64_000
START_TIMEOUT_SECONDS = 20
CALL_TIMEOUT_SECONDS = 15 * 60


class TelephonyError(RuntimeError):
    """A provider call could not be placed or authenticated."""


def stream_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _public_url(settings: Settings, path: str, query_string: str = "") -> str:
    url = f"{settings.public_base_url.rstrip('/')}{path}"
    return f"{url}?{query_string}" if query_string else url


def twilio_signature(url: str, params: list[tuple[str, str]], auth_token: str) -> str:
    """Compute Twilio's HMAC-SHA1 webhook signature for an exact public URL."""
    payload = url + "".join(key + value for key, value in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def validate_twilio_http_request(request: Request) -> bool:
    settings = request.app.state.settings
    if settings.telephony_provider != "twilio" or not settings.twilio_auth_token:
        return False
    body = await request.body()
    params = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    url = _public_url(settings, request.url.path, request.url.query)
    expected = twilio_signature(url, params, settings.twilio_auth_token)
    return secrets.compare_digest(request.headers.get("x-twilio-signature", ""), expected)


def validate_twilio_websocket(websocket: WebSocket) -> bool:
    settings = websocket.app.state.settings
    if settings.telephony_provider != "twilio" or not settings.twilio_auth_token:
        return False
    websocket_base = settings.public_base_url.replace("https://", "wss://")
    url = f"{websocket_base.rstrip('/')}{websocket.url.path}"
    expected = twilio_signature(url, [], settings.twilio_auth_token)
    return secrets.compare_digest(websocket.headers.get("x-twilio-signature", ""), expected)


async def place_outbound_call(settings: Settings, store: Store, task_id: str) -> dict[str, Any]:
    """Dial one task through Twilio after atomically consuming an active consent."""
    if settings.telephony_provider != "twilio":
        raise TelephonyError("live telephony is not configured")
    raw_token = secrets.token_urlsafe(32)
    try:
        reservation = store.reserve_outbound_call(
            task_id, "twilio", stream_token_hash(raw_token),
            minimum_interval_days=settings.minimum_call_interval_days,
        )
    except (KeyError, ValueError) as error:
        store.release_task(task_id, str(error), actor="ontheline-caller")
        raise TelephonyError(str(error)) from error
    call_id = reservation["call_id"]
    token_query = urlencode({"token": raw_token, "call": call_id})
    twiml_url = _public_url(settings, f"/telephony/twiml/{quote(task_id)}", token_query)
    status_url = _public_url(settings, f"/telephony/status/{quote(call_id)}", token_query)
    api_url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{quote(settings.twilio_account_sid)}/Calls.json"
    )
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                api_url,
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                data={
                    "To": reservation["destination"],
                    "From": settings.twilio_from_number,
                    "Url": twiml_url,
                    "Method": "POST",
                    "StatusCallback": status_url,
                    "StatusCallbackMethod": "POST",
                    "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
                },
            )
            response.raise_for_status()
            payload = response.json()
            external_sid = payload["sid"]
    except (httpx.HTTPError, KeyError, ValueError) as error:
        store.update_outbound_call(call_id, "failed", failure_reason=str(error))
        store.release_task(task_id, str(error), actor="ontheline-caller")
        raise TelephonyError("the telephony provider rejected the call") from error
    store.update_outbound_call(call_id, "dialing", external_sid=external_sid)
    return {"call_id": call_id, "task_id": task_id, "status": "dialing", "provider_sid": external_sid}


SUPPORTED_OUTCOMES = {
    "complete", "voicemail", "no_answer", "wrong_number", "stop_requested",
    "reached_client", "distress", "hostile", "ambiguous",
}

CLOSURE_WORDS = ("closed", "ended", "stopped", "inactive", "suspended")


def _summarise(outcome: str, notes: list[str]) -> str:
    summary = f"Live provider call ended with {outcome}."
    for note in notes:
        if len(summary) + len(note) + 1 > 990:
            break
        summary = f"{summary} {note}"
    return summary


def call_record_to_extraction(record: CallRecord) -> ExtractionResult:
    """Turn one call's in-memory record into the schema the tier policy consumes.

    The voice agent writes this record through its tools, and one of the things
    it writes is `read_back_confirmed`: its own account of whether the provider
    confirmed a readback. The tier policy treats that as evidence, and publishes
    a low-judgment change without a curator when it is true. So a wrong tool call
    used to be enough to publish, because the model was the only witness to its
    own conversation.

    Every such claim is checked here against the recorded transcript before
    policy sees it. `read_back_confirmed` survives only if the transcript shows
    the provider raising the value, the assistant reading the whole of it back,
    and the provider agreeing. `explicitly_confirmed` survives only if the quote
    the agent attributed to the provider resembles something the provider is
    recorded as saying. What the model can still do is send a curator something
    to look at. What it can no longer do is publish.
    """
    changes: list[ProposedFieldChange] = []
    notes: list[str] = []
    field_aliases = {"eligibility_description": "eligibility"}
    for item in record.proposed_changes:
        field = field_aliases.get(item["field"], item["field"])
        new_value = str(item["new_value"])
        claimed = bool(item.get("read_back_confirmed"))
        evidence = corroborate(record.transcript, field, new_value)
        quoted = quote_supported(record.transcript, str(item.get("quote", "")))
        confirmed = claimed and evidence.corroborated
        if claimed and not evidence.corroborated:
            notes.append(
                f"{field}: the call agent reported a confirmed readback that the transcript does "
                f"not show, so it is going to a person ({evidence.reason})."
            )
        elif confirmed:
            notes.append(f"{field}: readback corroborated in the transcript.")
        else:
            notes.append(f"{field}: no readback was claimed, so it is going to a person.")
        removes = field == "status" and any(word in new_value.lower() for word in CLOSURE_WORDS)
        changes.append(ProposedFieldChange(
            field=field,
            new_value=item["new_value"],
            quote=item["quote"],
            confidence=0.99 if confirmed else 0.5,
            explicitly_confirmed=quoted,
            read_back_confirmed=confirmed,
            removes_service=removes,
            ambiguous=not confirmed,
        ))
    outcome = record.end_reason if record.end_reason in SUPPORTED_OUTCOMES else "ambiguous"
    return ExtractionResult(
        confirmed_fields=record.confirmed_unchanged,
        changes=changes,
        summary=_summarise(outcome, notes),
        outcome=outcome,
        disclosure_spoken=record.disclosure_spoken,
        recording_consent=False,
    )


def _needs_review(extraction: ExtractionResult, reason: str) -> ExtractionResult:
    """Send every proposed change to a curator, keeping the evidence intact."""
    downgraded = extraction.model_copy(deep=True)
    for change in downgraded.changes:
        change.read_back_confirmed = False
        change.ambiguous = True
        change.confidence = min(change.confidence, 0.5)
    downgraded.summary = _summarise(downgraded.outcome, [reason])[:1000]
    return downgraded


def reconcile_extractions(
    from_call: ExtractionResult, independent: ExtractionResult, trace: list[dict[str, str]]
) -> ExtractionResult:
    """Require two independent readings of the same call to agree before publishing.

    ``from_call`` is what the voice agent recorded while it was talking.
    ``independent`` is what the safety-reader and extractor graph made of the
    finished transcript, having taken no part in the conversation. A change
    publishes without a curator only where both agree it happened and agree what
    it says.

    Disagreement is never resolved in favour of either side. It is recorded and
    the change goes to a person, which is the same rule the rest of this product
    follows: the machine narrows the queue, it does not decide.
    """
    merged = from_call.model_copy(deep=True)
    second = {change.field: change for change in independent.changes}

    for change in merged.changes:
        other = second.get(change.field)
        if other is None:
            change.read_back_confirmed = False
            change.ambiguous = True
            change.confidence = min(change.confidence, 0.5)
            trace.append({
                "agent": "reconciliation",
                "event": f"{change.field}.only-the-call-agent-saw-this",
            })
        elif str(other.new_value).strip().lower() != str(change.new_value).strip().lower():
            change.read_back_confirmed = False
            change.ambiguous = True
            change.confidence = min(change.confidence, 0.5)
            trace.append({
                "agent": "reconciliation",
                "event": f"{change.field}.the-two-readings-disagree",
            })
        elif not (other.read_back_confirmed and other.explicitly_confirmed):
            change.read_back_confirmed = False
            change.ambiguous = True
            change.confidence = min(change.confidence, 0.5)
            trace.append({
                "agent": "reconciliation",
                "event": f"{change.field}.the-transcript-reading-found-no-confirmation",
            })
        else:
            trace.append({"agent": "reconciliation", "event": f"{change.field}.both-readings-agree"})

    # Anything only the transcript reading found still belongs in front of a
    # person. A change the voice agent forgot to record is exactly the kind of
    # thing nobody would ever discover afterwards.
    seen = {change.field for change in merged.changes}
    for change in independent.changes:
        if change.field in seen or change.field in merged.confirmed_fields:
            continue
        extra = change.model_copy(deep=True)
        extra.read_back_confirmed = False
        extra.ambiguous = True
        extra.confidence = min(extra.confidence, 0.5)
        merged.changes.append(extra)
        trace.append({
            "agent": "reconciliation",
            "event": f"{change.field}.only-the-transcript-reading-saw-this",
        })

    # Either reading may stop the call. Neither may un-stop it.
    if independent.outcome in SAFETY_OUTCOMES and merged.outcome not in SAFETY_OUTCOMES:
        merged.outcome = independent.outcome
        trace.append({
            "agent": "reconciliation",
            "event": f"outcome.escalated-to-{independent.outcome}",
        })

    merged.summary = _summarise(
        merged.outcome,
        ["The finished transcript was read independently and the two readings were reconciled."],
    )
    return merged


async def verified_extraction(
    settings: Settings, task: dict[str, Any], record: CallRecord, trace: list[dict[str, str]]
) -> ExtractionResult:
    """The evidence a live call produces, after everything that checks it has run.

    Two things sit between the voice agent and the write gate. The transcript
    corroboration in :mod:`ontheline.readback` runs always, including with no
    credentials at all. The independent extraction graph runs wherever a model
    path is configured, and it reads the finished transcript exactly as the text
    workflow does, which is what the architecture has always said happens.
    """
    from_call = call_record_to_extraction(record)
    if settings.agent_mode == "demo":
        trace.append({"agent": "reconciliation", "event": "transcript-corroboration.only"})
        return from_call
    if not record.transcript:
        return _needs_review(from_call, "No transcript was captured, so nothing could be checked.")

    transcript = [
        TranscriptTurn(role=turn["role"], text=turn["text"])
        for turn in record.transcript
        if turn.get("text")
    ]
    try:
        independent = await asyncio.to_thread(
            extract_from_transcript, settings, task, transcript, trace
        )
    except Exception as error:  # a reading that did not happen is not a reading that agreed
        trace.append({
            "agent": "hsds-extractor",
            "event": f"independent-extraction.unavailable:{type(error).__name__}",
        })
        return _needs_review(
            from_call,
            "The finished transcript could not be read independently, so every change is "
            "going to a person.",
        )
    return reconcile_extractions(from_call, independent, trace)


async def twiml(request: Request) -> Response:
    """Return TwiML only for a signed callback and its one-time reserved call token."""
    if not await validate_twilio_http_request(request):
        return Response("Not authorized", status_code=401)
    settings = request.app.state.settings
    raw_token = request.query_params.get("token", "")
    call_id = request.query_params.get("call", "")
    call = request.app.state.store.outbound_call_by_token(stream_token_hash(raw_token))
    task_id = request.path_params["task_id"]
    if not call or call["id"] != call_id or call["task_id"] != task_id:
        return Response("Not authorized", status_code=401)
    if call["status"] not in ("reserved", "dialing"):
        return Response("Call is not active", status_code=409)
    websocket_base = settings.public_base_url.replace("https://", "wss://")
    stream_url = escape(f"{websocket_base.rstrip('/')}/telephony/twilio/{task_id}")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Connect><Stream name="ontheline-provider" url="' + stream_url + '">'
        '<Parameter name="token" value="' + escape(raw_token) + '"/>'
        '<Parameter name="call_id" value="' + escape(call_id) + '"/>'
        '</Stream></Connect></Response>'
    )
    return Response(xml, media_type="application/xml")


async def intake_twiml(request: Request) -> Response:
    """Return the authenticated TwiML entry point for inbound seeker calls."""
    if not await validate_twilio_http_request(request):
        return Response("Not authorized", status_code=401)
    settings = request.app.state.settings
    websocket_base = settings.public_base_url.replace("https://", "wss://")
    stream_url = escape(f"{websocket_base.rstrip('/')}/telephony/intake/twilio")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Connect><Stream name="ontheline-intake" url="' + stream_url
        + '"/></Connect></Response>'
    )
    return Response(xml, media_type="application/xml")


async def twilio_status(request: Request) -> Response:
    """Apply signed provider lifecycle callbacks and recover tasks after failed dialing."""
    if not await validate_twilio_http_request(request):
        return Response("Not authorized", status_code=401)
    raw_token = request.query_params.get("token", "")
    call_id = request.path_params["call_id"]
    call = request.app.state.store.outbound_call_by_token(stream_token_hash(raw_token))
    if not call or call["id"] != call_id:
        return Response("Not authorized", status_code=401)
    form = dict(parse_qsl((await request.body()).decode("utf-8"), keep_blank_values=True))
    twilio_status_value = form.get("CallStatus", "").lower()
    external_sid = form.get("CallSid") or None
    if twilio_status_value in {"busy", "failed", "no-answer", "canceled"}:
        request.app.state.store.update_outbound_call(
            call_id, "failed" if twilio_status_value != "canceled" else "cancelled",
            external_sid=external_sid, failure_reason=twilio_status_value,
        )
        request.app.state.store.reschedule_task(
            call["task_id"], twilio_status_value, actor="telephony",
            minimum_interval_days=request.app.state.settings.minimum_call_interval_days,
        )
    return Response(status_code=204)


async def twilio_media(websocket: WebSocket) -> None:
    """Bridge a signed Twilio media stream into Nova Sonic and the policy/store."""
    if not validate_twilio_websocket(websocket):
        await websocket.close(code=1008, reason="invalid provider signature")
        return
    await websocket.accept()
    settings = websocket.app.state.settings
    task_id = websocket.path_params["task_id"]
    call_id = ""
    task = websocket.app.state.store.get_task(task_id)
    if not task or task["status"] != "in_progress" or task["do_not_call"]:
        await websocket.close(code=1008, reason="verification task is not callable")
        return

    stream_sid = ""
    call: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(START_TIMEOUT_SECONDS):
            while not stream_sid:
                message = await websocket.receive_text()
                if len(message) > MAX_MEDIA_MESSAGE_BYTES:
                    raise ValueError("media control message is too large")
                event = json.loads(message)
                if event.get("event") != "start":
                    continue
                start = event.get("start", {})
                custom = start.get("customParameters", {})
                raw_token = custom.get("token", "")
                call_id = custom.get("call_id", "")
                stream_sid = start.get("streamSid", "")
                if not stream_sid:
                    raise ValueError("missing stream SID")
                call = websocket.app.state.store.consume_outbound_media_token(
                    stream_token_hash(raw_token), call_id, task_id
                )
                if not call:
                    await websocket.close(code=1008, reason="invalid or consumed media token")
                    return
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, ValueError):
        return

    service = websocket.app.state.store.service(task["service_id"])
    if not service:
        websocket.app.state.store.update_outbound_call(call_id, "failed", failure_reason="service not found")
        websocket.app.state.store.release_task(task_id, "service not found", actor="telephony")
        await websocket.close(code=1011, reason="service not found")
        return
    fields = {field: str(service.get(field) or "not recorded") for field in task["fields"]}
    record = CallRecord(org_name=task["organization_name"], fields_in_question=fields)
    audio_input = TelephonyAudioInput()

    async def send_frame(frame: bytes) -> None:
        await websocket.send_json({
            "event": "media", "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        })

    async def capture_transcript(event: dict[str, Any]) -> None:
        text = event.get("text") or event.get("delta", {}).get("text", "")
        if not text:
            return
        role = event.get("role", "system")
        role = role if role in {"assistant", "provider", "seeker", "system"} else "system"
        record.transcript.append({"role": role, "text": redact_pii(text)})
        if role == "assistant" and "automated assistant" in text.lower():
            record.disclosure_spoken = True

    audio_output = TelephonyAudioOutput(send_frame, on_transcript=capture_transcript)
    agent = build_agent(record, directory_name=settings.directory_name, region=settings.aws_region)

    async def receive_media() -> None:
        while True:
            message = await websocket.receive_text()
            if len(message) > MAX_MEDIA_MESSAGE_BYTES:
                raise ValueError("media message is too large")
            event = json.loads(message)
            if event.get("event") == "media" and event.get("media", {}).get("track", "inbound") == "inbound":
                try:
                    payload = base64.b64decode(event["media"]["payload"], validate=True)
                except (KeyError, binascii.Error) as error:
                    raise ValueError("invalid media payload") from error
                if not payload or len(payload) > 8000:
                    raise ValueError("invalid media frame size")
                audio_input.push(payload)
            elif event.get("event") == "stop":
                return

    agent_task = asyncio.create_task(agent.run(inputs=[audio_input], outputs=[audio_output]))
    receive_task = asyncio.create_task(receive_media())
    bridge_error: Exception | None = None
    try:
        async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
            done, pending = await asyncio.wait(
                {agent_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for running in pending:
                running.cancel()
            for completed in done:
                if not completed.cancelled():
                    completed.result()
    except (WebSocketDisconnect, TimeoutError, asyncio.CancelledError, ValueError, json.JSONDecodeError) as error:
        bridge_error = error
        record.end_reason = record.end_reason or "ambiguous"
    finally:
        for running in (agent_task, receive_task):
            if not running.done():
                running.cancel()
        await asyncio.gather(agent_task, receive_task, return_exceptions=True)

    trace: list[dict[str, str]] = [
        {"agent": "nova-sonic-provider-caller", "event": "bidirectional-call.completed"}
    ]
    try:
        extraction = await verified_extraction(settings, task, record, trace)
        if extraction.outcome in {"voicemail", "no_answer"}:
            websocket.app.state.store.reschedule_task(
                task_id, extraction.outcome, actor="ontheline-voice-agent",
                minimum_interval_days=settings.minimum_call_interval_days,
            )
            websocket.app.state.store.update_outbound_call(call_id, "completed")
            await websocket.close(code=1000)
            return
        websocket.app.state.store.record_verification(
            task_id, extraction, classify_extraction(extraction, task["fields"]), record.transcript,
            actor="ontheline-voice-agent", agent_trace=trace,
        )
        websocket.app.state.store.update_outbound_call(call_id, "completed")
    except Exception as error:
        websocket.app.state.store.update_outbound_call(call_id, "failed", failure_reason=str(error))
        websocket.app.state.store.release_task(task_id, str(error), actor="telephony")
        bridge_error = bridge_error or error
    try:
        await websocket.close(code=1011 if bridge_error else 1000)
    except RuntimeError:
        pass


async def twilio_intake_media(websocket: WebSocket) -> None:
    """Bridge an authenticated inbound Twilio stream to the Nova 2 Sonic intake agent."""
    if not validate_twilio_websocket(websocket):
        await websocket.close(code=1008, reason="invalid provider signature")
        return
    await websocket.accept()
    stream_sid = ""
    try:
        async with asyncio.timeout(START_TIMEOUT_SECONDS):
            while not stream_sid:
                message = await websocket.receive_text()
                if len(message) > MAX_MEDIA_MESSAGE_BYTES:
                    raise ValueError("media control message is too large")
                event = json.loads(message)
                if event.get("event") == "start":
                    stream_sid = event.get("start", {}).get("streamSid", "")
                    if not stream_sid:
                        raise ValueError("missing stream SID")
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, ValueError):
        return

    audio_input = TelephonyAudioInput()

    async def send_frame(frame: bytes) -> None:
        await websocket.send_json({
            "event": "media", "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        })

    async def receive_media() -> None:
        while True:
            message = await websocket.receive_text()
            if len(message) > MAX_MEDIA_MESSAGE_BYTES:
                raise ValueError("media message is too large")
            event = json.loads(message)
            if event.get("event") == "media" and event.get("media", {}).get("track", "inbound") == "inbound":
                try:
                    payload = base64.b64decode(event["media"]["payload"], validate=True)
                except (KeyError, binascii.Error) as error:
                    raise ValueError("invalid media payload") from error
                if not payload or len(payload) > 8000:
                    raise ValueError("invalid media frame size")
                audio_input.push(payload)
            elif event.get("event") == "stop":
                return

    settings = websocket.app.state.settings
    audio_output = TelephonyAudioOutput(send_frame)
    agent = build_intake_agent(
        websocket.app.state.store,
        IntakeRecord(),
        settings.directory_name,
        settings.aws_region,
    )
    agent_task = asyncio.create_task(agent.run(inputs=[audio_input], outputs=[audio_output]))
    receive_task = asyncio.create_task(receive_media())
    bridge_error = False
    try:
        async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
            done, pending = await asyncio.wait(
                {agent_task, receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for running in pending:
                running.cancel()
            for completed in done:
                if not completed.cancelled():
                    completed.result()
    except (WebSocketDisconnect, TimeoutError, asyncio.CancelledError, ValueError, json.JSONDecodeError):
        bridge_error = True
    finally:
        for running in (agent_task, receive_task):
            if not running.done():
                running.cancel()
        await asyncio.gather(agent_task, receive_task, return_exceptions=True)
    try:
        await websocket.close(code=1011 if bridge_error else 1000)
    except RuntimeError:
        pass
