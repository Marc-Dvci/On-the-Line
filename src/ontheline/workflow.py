"""Strands extraction graph plus a deterministic, offline demonstration adapter."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import boto3
from strands import Agent
from strands.hooks import AfterInvocationEvent, BeforeInvocationEvent, HookRegistry
from strands.models import BedrockModel
from strands.multiagent import GraphBuilder, Status
from strands.session import FileSessionManager
from strands.vended_plugins.goal import GoalLoop

from ontheline.config import Settings
from ontheline.database import Store
from ontheline.domain import ExtractionResult, ProposedFieldChange, TranscriptTurn
from ontheline.policy import classify_extraction
from ontheline.privacy import redact_pii

TRIAGE_PROMPT = """You are the safety reader for a community-resource verification call.
Preserve every provider statement and quote. Identify ambiguity, contradictions, distress, a help-seeker
answering instead of staff, and stop-calling requests. Never infer facts the provider did not say.
Your output is passed to a second agent, so include the complete transcript verbatim."""

EXTRACT_PROMPT = """Extract a verification result from the safety reader's report.
Only propose a field change when the provider states it explicitly. A read_back_confirmed change requires
the provider to confirm the assistant's exact readback. Mark any closure, removal, reduced availability,
eligibility change, uncertainty, or contradiction for review. Quotes must be verbatim. Never invent values.
The deterministic application policy runs after you and will reject unsafe writes."""


DEMO_RESULTS: dict[str, ExtractionResult] = {
    "confirmed": ExtractionResult(
        confirmed_fields=["schedule", "phone"], changes=[],
        summary="Provider confirmed the schedule and phone number without changes.", outcome="complete",
        disclosure_spoken=True,
    ),
    "hours_changed": ExtractionResult(
        confirmed_fields=["phone"],
        changes=[ProposedFieldChange(
            field="schedule", new_value="WE 10:00-13:00",
            quote="Yes, ten to one on Wednesdays is correct.", confidence=0.99,
            explicitly_confirmed=True, read_back_confirmed=True
        )], summary="Provider changed Wednesday pickup hours and confirmed the readback.", outcome="complete",
        disclosure_spoken=True,
    ),
    "program_closed": ExtractionResult(
        changes=[ProposedFieldChange(
            field="status", new_value="inactive", quote="That program closed at the end of July.",
            confidence=0.99, explicitly_confirmed=True, read_back_confirmed=True, removes_service=True
        )], summary="Provider reported that the program closed; human approval is required.", outcome="complete",
        disclosure_spoken=True,
    ),
    "uncertain_volunteer": ExtractionResult(
        changes=[ProposedFieldChange(
            field="schedule", new_value="Possibly Thursday mornings",
            quote="I think it might be Thursday now, but I would have to check.", confidence=0.41,
            ambiguous=True
        )], summary="The volunteer was unsure about the current hours.", outcome="ambiguous",
        disclosure_spoken=True,
    ),
    "stop_calling": ExtractionResult(
        changes=[], summary="The organization asked not to receive another verification call.",
        outcome="stop_requested", disclosure_spoken=True,
    ),
}


class InvocationTraceHooks:
    """Capture the model lifecycle used to produce a verification proposal."""

    def __init__(self, trace: list[dict[str, str]]):
        self.trace = trace

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.before_invocation)
        registry.add_callback(AfterInvocationEvent, self.after_invocation)

    def before_invocation(self, event: BeforeInvocationEvent) -> None:
        self.trace.append({"agent": event.agent.name, "event": "invocation.started"})

    def after_invocation(self, event: AfterInvocationEvent) -> None:
        self.trace.append({"agent": event.agent.name, "event": "invocation.completed"})


def extraction_goal(response: dict[str, Any], agent: Agent) -> dict[str, Any]:
    """GoalLoop validator for domain-valid structured extraction, beyond JSON shape."""
    tool_inputs = [
        block["toolUse"]["input"]
        for block in response.get("content", [])
        if block.get("toolUse", {}).get("name") == "ExtractionResult"
    ]
    if not tool_inputs:
        return {"passed": False, "feedback": "Return the final answer with the ExtractionResult tool."}
    try:
        result = ExtractionResult.model_validate(tool_inputs[-1])
    except Exception as error:
        return {"passed": False, "feedback": f"ExtractionResult is invalid: {error}"}
    for change in result.changes:
        if change.field == "status" and str(change.new_value).lower() != "active" and not change.removes_service:
            return {
                "passed": False,
                "feedback": "A non-active status must set removes_service=true.",
            }
        if change.read_back_confirmed and not change.explicitly_confirmed:
            return {
                "passed": False,
                "feedback": "Readback confirmation requires explicitly_confirmed=true.",
            }
    return {"passed": True}


def run_strands_extraction(
    settings: Settings, task: dict[str, Any], transcript: list[TranscriptTurn],
    trace: list[dict[str, str]],
) -> ExtractionResult:
    """Run the load-bearing Strands Graph, GoalLoop, hooks, and persisted session."""
    model = BedrockModel(
        region_name=settings.aws_region,
        model_id=settings.bedrock_model_id,
        temperature=0,
    )
    hooks = InvocationTraceHooks(trace)
    triage = Agent(
        model=model, name="safety-reader", system_prompt=TRIAGE_PROMPT,
        callback_handler=None, hooks=[hooks],
    )
    goal = GoalLoop(goal=extraction_goal, max_attempts=3, timeout=90.0)
    extractor = Agent(
        model=model, name="hsds-extractor", system_prompt=EXTRACT_PROMPT,
        structured_output_model=ExtractionResult, callback_handler=None,
        hooks=[hooks], plugins=[goal],
    )
    builder = GraphBuilder()
    builder.add_node(triage, "triage")
    builder.add_node(extractor, "extractor")
    builder.add_edge("triage", "extractor")
    builder.set_entry_point("triage")
    builder.set_graph_id(f"verification-{task['id']}")
    builder.set_max_node_executions(2)
    builder.set_execution_timeout(120)
    builder.set_session_manager(FileSessionManager(
        session_id=f"verification-{task['id']}", storage_dir=str(settings.agent_session_path)
    ))
    graph = builder.build()
    prompt = json.dumps({
        "organization": task["organization_name"], "service": task["service_name"],
        "fields_to_verify": task["fields"],
        "transcript": [turn.model_dump(mode="json") for turn in transcript],
    }, ensure_ascii=False)
    graph_result = graph(prompt)
    if graph_result.status != Status.COMPLETED:
        raise RuntimeError(f"Strands verification graph ended as {graph_result.status.value}")
    node_result = graph_result.results["extractor"].result
    structured = getattr(node_result, "structured_output", None)
    if not isinstance(structured, ExtractionResult):
        raise RuntimeError("Strands extractor did not return a schema-valid result")
    goal_result = goal.last_result(extractor)
    if not goal_result or not goal_result.passed:
        raise RuntimeError("Strands GoalLoop did not satisfy extraction invariants")
    return structured


class VerificationWorkflow:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings

    def run(
        self, task_id: str, transcript: list[TranscriptTurn], demo_scenario: str | None = None
    ) -> dict[str, Any]:
        task = self.store.claim_task(task_id, "ontheline-workflow")
        if task["do_not_call"]:
            self.store.release_task(task_id, "organization has permanently opted out of calls")
            raise ValueError("organization has permanently opted out of calls")
        trace: list[dict[str, str]] = []
        try:
            if self.settings.agent_mode == "bedrock":
                extraction = run_strands_extraction(self.settings, task, transcript, trace)
            elif self.settings.agent_mode == "agentcore":
                extraction = self._run_agentcore(task, transcript, trace)
            else:
                extraction = self._offline_extract(transcript, demo_scenario)
            decisions = classify_extraction(extraction, task["fields"])
            result = self.store.record_verification(
                task_id, extraction, decisions,
                [
                    {**turn.model_dump(mode="json"), "text": redact_pii(turn.text)}
                    for turn in transcript
                ],
                agent_trace=trace,
            )
        except Exception as error:
            self.store.release_task(task_id, str(error))
            raise
        result["extraction"] = extraction.model_dump(mode="json")
        result["decisions"] = result.pop("policy_decisions")
        return result

    def _run_strands(
        self, task: dict[str, Any], transcript: list[TranscriptTurn], trace: list[dict[str, str]]
    ) -> ExtractionResult:
        return run_strands_extraction(self.settings, task, transcript, trace)

    def _run_agentcore(
        self, task: dict[str, Any], transcript: list[TranscriptTurn], trace: list[dict[str, str]]
    ) -> ExtractionResult:
        """Invoke the stateless extraction runtime; policy and persistence stay local."""
        payload = json.dumps({"input": {
            "session_id": task["id"],
            "organization_name": task["organization_name"],
            "service_name": task["service_name"],
            "fields": task["fields"],
            "transcript": [turn.model_dump(mode="json") for turn in transcript],
        }}).encode()
        client = boto3.client("bedrock-agentcore", region_name=self.settings.aws_region)
        response = client.invoke_agent_runtime(
            agentRuntimeArn=self.settings.agentcore_runtime_arn,
            runtimeSessionId=hashlib.sha256(task["id"].encode()).hexdigest(),
            qualifier="DEFAULT",
            payload=payload,
        )
        try:
            output = json.loads(response["response"].read())
            runtime_output = output["output"]
            trace.extend(runtime_output.get("trace", []))
            return ExtractionResult.model_validate(runtime_output["extraction"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("AgentCore returned an invalid extraction response") from error

    @staticmethod
    def _offline_extract(transcript: list[TranscriptTurn], scenario: str | None) -> ExtractionResult:
        """Credential-free demo mode. Unknown language is escalated, never guessed."""
        if scenario:
            return DEMO_RESULTS[scenario].model_copy(deep=True)
        provider_text = " ".join(
            turn.text for turn in transcript if turn.role in ("provider", "seeker")
        )
        lowered = provider_text.lower()
        if any(phrase in lowered for phrase in ("stop calling", "take us off", "do not call")):
            return DEMO_RESULTS["stop_calling"].model_copy(deep=True)
        if any(phrase in lowered for phrase in ("need food", "help for my", "looking for help")):
            return ExtractionResult(
                summary="A help-seeker, not provider staff, answered the line.",
                outcome="reached_client", disclosure_spoken=True,
            )
        if any(phrase in lowered for phrase in ("closed", "shut down", "stopped the program")):
            result = DEMO_RESULTS["program_closed"].model_copy(deep=True)
            result.changes[0].quote = provider_text[:1000]
            return result
        if re.search(r"\b(unchanged|still the same|that is correct)\b", lowered):
            return DEMO_RESULTS["confirmed"].model_copy(deep=True)
        return ExtractionResult(
            summary="Offline mode could not establish an explicit, confirmed fact; sent to a curator.",
            outcome="ambiguous", disclosure_spoken=True,
        )


CALL_SCRIPTS: dict[str, list[tuple[str, str]]] = {
    "hours_changed": [
        ("assistant",
         "Hello, this is On the Line. I am an automated assistant calling for the community "
         "resource directory. Is now a good time to check what you publish for the weekly "
         "grocery pickup?"),
        ("provider", "Sure, go ahead."),
        ("assistant",
         "Thank you. We list it as Wednesdays, nine to twelve, on the number ending one one zero "
         "one. Is that still right?"),
        ("provider",
         "The phone number is right. The hours moved though. They changed to ten to one."),
        ("assistant",
         "Let me read that back. Wednesdays, ten in the morning to one in the afternoon. "
         "Have I got that right?"),
        ("provider", "Yes, ten to one on Wednesdays is correct."),
        ("assistant", "Thank you, I have noted it. Have a good afternoon."),
    ],
    "confirmed": [
        ("assistant",
         "Hello, this is On the Line, an automated assistant calling on behalf of the community "
         "resource directory. I am checking the details you publish for the weekly grocery pickup."),
        ("provider", "Go ahead."),
        ("assistant",
         "We list the pickup as Wednesdays, nine to twelve, on five five five, zero one zero, "
         "one one zero one. Is any of that out of date?"),
        ("provider", "No, that is correct. The Wednesday hours and that phone number are unchanged."),
        ("assistant", "Thank you, I will note that nothing has changed."),
    ],
    "program_closed": [
        ("assistant",
         "Hello, this is On the Line, an automated assistant calling on behalf of the community "
         "resource directory. I am checking the details you publish for the weekly grocery pickup."),
        ("provider", "Oh. That program closed at the end of July."),
        ("assistant",
         "I understand, thank you for telling me. To confirm, the weekly grocery pickup is no "
         "longer running."),
        ("provider", "That is right, it ended in July."),
        ("assistant",
         "Thank you. I will not change the listing myself. Someone on the directory team will "
         "review this before anything is removed."),
    ],
    "uncertain_volunteer": [
        ("assistant",
         "Hello, this is On the Line, an automated assistant calling on behalf of the community "
         "resource directory. I am checking the details you publish for the weekly grocery pickup."),
        ("provider", "I am just a volunteer, but I can try."),
        ("assistant", "Thank you. We list the pickup as Wednesdays. Is that still the day?"),
        ("provider", "I think it might be Thursday now, but I would have to check."),
        ("assistant",
         "That is completely fine. I will not record anything uncertain, and I will leave it for "
         "someone to confirm. Thank you for your time."),
    ],
    "stop_calling": [
        ("assistant",
         "Hello, this is On the Line, an automated assistant calling on behalf of the community "
         "resource directory. I am checking the details you publish for the weekly grocery pickup."),
        ("provider", "Please stop calling and take us off your list."),
        ("assistant",
         "Understood. I am ending the call now and this number will not be called again."),
    ],
}


def demo_transcript(scenario: str) -> list[TranscriptTurn]:
    return [TranscriptTurn(role=role, text=text) for role, text in CALL_SCRIPTS[scenario]]
