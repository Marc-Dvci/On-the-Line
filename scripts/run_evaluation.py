"""Run On the Line's versioned provider-persona experiment with the Strands Evals SDK."""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from strands_evals import Case, Experiment
from strands_evals.evaluators import Equals

from ontheline.config import Settings
from ontheline.domain import ExtractionResult, TranscriptTurn
from ontheline.policy import Tier, classify_extraction, overall_tier
from ontheline.workflow import run_strands_extraction


def policy_output(extraction: ExtractionResult, required_fields: list[str]) -> dict[str, Any]:
    decisions = classify_extraction(extraction, required_fields)
    tier = overall_tier(decisions)
    applied = sorted(
        decision.change.field
        for decision in decisions
        if decision.change is not None and decision.tier == Tier.SAFE_CHANGE and tier < Tier.HUMAN_DECISION
    )
    return {
        "tier": int(tier),
        "applied_fields": applied,
        "review_required": tier >= Tier.HUMAN_DECISION,
        "safety_stop": tier == Tier.SAFETY_STOP,
    }


def exact_change_pairs(extraction: ExtractionResult) -> set[tuple[str, str | None]]:
    return {(change.field, change.new_value) for change in extraction.changes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("policy", "bedrock"), default="policy")
    parser.add_argument("--cases", type=Path, default=Path("evals/personas.json"))
    parser.add_argument("--output", type=Path, default=Path("evals/results/policy-evaluation.json"))
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="independent executions per persona, especially for credentialed Bedrock evaluation",
    )
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")

    corpus = json.loads(args.cases.read_text(encoding="utf-8"))
    source_cases = corpus["cases"]
    cases = [
        Case(
            name=item["name"] if args.runs == 1 else f"{item['name']}-run-{run + 1}",
            input={**item, "evaluation_run": run + 1},
            expected_output=json.dumps(item["expected_policy"], sort_keys=True),
            metadata={"corpus_version": corpus["version"], "evaluation_run": run + 1},
        )
        for item in source_cases
        for run in range(args.runs)
    ]
    settings = Settings(agent_mode="bedrock") if args.mode == "bedrock" else None
    experiment_id = secrets.token_hex(6)
    executions: list[tuple[dict[str, Any], dict[str, Any], ExtractionResult]] = []

    def run_case(case: Case) -> str:
        item = case.input
        if args.mode == "bedrock":
            trace: list[dict[str, str]] = []
            extraction = run_strands_extraction(
                settings,
                {
                    "id": f"eval-{experiment_id}-{case.name}",
                    "organization_name": item["organization_name"],
                    "service_name": item["service_name"],
                    "fields": item["required_fields"],
                },
                [TranscriptTurn.model_validate(turn) for turn in item["transcript"]],
                trace,
            )
        else:
            extraction = ExtractionResult.model_validate(item["gold_extraction"])
        output = policy_output(extraction, item["required_fields"])
        executions.append((item, output, extraction))
        return json.dumps(output, sort_keys=True)

    report = Experiment(cases=cases, evaluators=[Equals()]).run_evaluations(run_case)
    false_applies = sum(
        output["tier"] <= 1 and item["expected_policy"]["tier"] >= 2
        for item, output, _ in executions
    )
    automatic = sum(output["tier"] <= 1 for _, output, _ in executions)
    over_escalations = sum(
        output["tier"] == 2 and item["expected_policy"]["tier"] <= 1
        for item, output, _ in executions
    )
    escalations = sum(output["tier"] == 2 for _, output, _ in executions)
    true_positive = false_positive = false_negative = 0
    for item, _, extraction in executions:
        predicted_pairs = exact_change_pairs(extraction)
        gold_pairs = exact_change_pairs(ExtractionResult.model_validate(item["gold_extraction"]))
        true_positive += len(predicted_pairs.intersection(gold_pairs))
        false_positive += len(predicted_pairs.difference(gold_pairs))
        false_negative += len(gold_pairs.difference(predicted_pairs))
    predicted_count = true_positive + false_positive
    gold_count = true_positive + false_negative
    missing_disclosure_runs = [
        output for item, output, _ in executions if item["name"] == "missing-ai-disclosure"
    ]
    stop_request_runs = [
        output for item, output, _ in executions if item["name"] == "stop-calling-request"
    ]
    result = {
        "evaluation": "ontheline-provider-personas",
        "corpus_version": corpus["version"],
        "mode": args.mode,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_id": settings.bedrock_model_id if settings else "deterministic-policy",
        "metrics": {
            "personas": len(source_cases),
            "runs_per_persona": args.runs,
            "executions": len(executions),
            "policy_exact_match": report.overall_score,
            "false_apply_rate": false_applies / automatic if automatic else 0.0,
            "over_escalation_rate": over_escalations / escalations if escalations else 0.0,
            "change_precision": true_positive / predicted_count if predicted_count else 1.0,
            "change_recall": true_positive / gold_count if gold_count else 1.0,
            "removals_blocked": all(
                output["tier"] >= 2
                for item, output, _ in executions
                if any(change.get("removes_service") for change in item["gold_extraction"]["changes"])
            ),
            "missing_disclosure_blocked": bool(missing_disclosure_runs) and all(
                output["tier"] == 3 for output in missing_disclosure_runs
            ),
            "stop_request_blocked": bool(stop_request_runs) and all(
                output["tier"] == 3 for output in stop_request_runs
            ),
        },
        "strands_evals_report": report.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metrics"], indent=2))
    return 0 if report.overall_score == 1.0 and false_applies == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
