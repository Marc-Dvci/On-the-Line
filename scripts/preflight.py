"""Verify the complete production configuration and its external credentials."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import secrets
import sqlite3
import sys
from pathlib import Path

import boto3
import httpx
from pydantic import ValidationError

from ontheline.config import Settings
from ontheline.domain import ExtractionResult


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, condition: bool, title: str, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {title}")
        if detail:
            print(f"       {detail}")
        if not condition:
            self.failures += 1


def load_settings(report: Report, env_file: Path) -> Settings | None:
    try:
        settings = Settings(_env_file=env_file)
    except ValidationError as error:
        report.check(False, "configuration", str(error).replace("\n", " ")[:700])
        return None
    report.check(settings.environment == "production", "production environment selected")
    report.check(settings.agent_mode in {"bedrock", "agentcore"}, "live extraction configured")
    report.check(settings.telephony_provider == "twilio", "Twilio telephony configured")
    return settings


def check_runtime(report: Report) -> None:
    report.check(sys.version_info >= (3, 12), "Python 3.12 or newer", sys.version.split()[0])
    for distribution in (
        "strands-agents", "aws-sdk-bedrock-runtime", "boto3", "starlette", "httpx"
    ):
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            report.check(False, f"package {distribution}")
        else:
            report.check(True, f"package {distribution}", version)


def check_storage(report: Report, settings: Settings) -> None:
    try:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.database_path) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        report.check(result == "ok", "database storage and integrity", str(settings.database_path))
    except Exception as error:
        report.check(False, "database storage and integrity", str(error)[:300])


def check_aws(report: Report, settings: Settings) -> None:
    try:
        session = boto3.Session(region_name=settings.aws_region)
        identity = session.client("sts").get_caller_identity()
        report.check(True, "AWS identity", identity["Arn"])
    except Exception as error:
        report.check(False, "AWS identity", f"{type(error).__name__}: {str(error)[:240]}")
        return
    try:
        bedrock = session.client("bedrock", region_name=settings.aws_region)
        profile = bedrock.get_inference_profile(
            inferenceProfileIdentifier=settings.bedrock_model_id
        )
        report.check(
            profile.get("status") == "ACTIVE",
            "Bedrock extraction inference profile",
            f"{settings.bedrock_model_id} ({profile.get('status', 'unknown')})",
        )
    except Exception as error:
        report.check(
            False, "Bedrock extraction inference profile",
            f"{type(error).__name__}: {str(error)[:240]}",
        )
    if settings.agent_mode == "agentcore":
        try:
            control = session.client("bedrock-agentcore-control", region_name=settings.aws_region)
            runtime_id = settings.agentcore_runtime_arn.rsplit("/", 1)[-1]
            runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
            report.check(
                runtime.get("status") == "READY", "AgentCore runtime",
                f"{runtime.get('agentRuntimeArn')} ({runtime.get('status', 'unknown')})",
            )
            metadata = runtime.get("metadataConfiguration") or {}
            report.check(
                metadata.get("requireMMDSV2") is True,
                "AgentCore MMDSv2 enforcement",
                "Required by AWS for runtime invocations since 30 June 2026.",
            )
        except Exception as error:
            report.check(False, "AgentCore runtime", f"{type(error).__name__}: {str(error)[:240]}")
            return
        try:
            data_plane = session.client("bedrock-agentcore", region_name=settings.aws_region)
            smoke_id = f"preflight-{secrets.token_hex(12)}"
            payload = json.dumps({"input": {
                "session_id": smoke_id,
                "organization_name": "On the Line deployment check",
                "service_name": "Runtime smoke test",
                "fields": ["phone"],
                "transcript": [
                    {
                        "role": "assistant",
                        "text": "I am On the Line, an automated assistant checking a directory listing.",
                    },
                    {"role": "provider", "text": "The listed phone number is still correct."},
                ],
            }}).encode()
            response = data_plane.invoke_agent_runtime(
                agentRuntimeArn=settings.agentcore_runtime_arn,
                runtimeSessionId=secrets.token_hex(32),
                qualifier="DEFAULT",
                payload=payload,
            )
            document = json.loads(response["response"].read())
            output = document["output"]
            extraction = ExtractionResult.model_validate(output["extraction"])
            report.check(
                bool(output.get("trace")) and extraction.disclosure_spoken,
                "AgentCore live extraction invocation",
                f"{extraction.outcome}; {len(output.get('trace', []))} lifecycle events",
            )
        except Exception as error:
            report.check(
                False,
                "AgentCore live extraction invocation",
                f"{type(error).__name__}: {str(error)[:240]}",
            )


def check_twilio(report: Report, settings: Settings) -> None:
    endpoint = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}.json"
    )
    try:
        response = httpx.get(
            endpoint,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            timeout=15,
        )
        response.raise_for_status()
        account = response.json()
        report.check(
            account.get("status") == "active", "Twilio account",
            f"{settings.twilio_account_sid} ({account.get('status', 'unknown')})",
        )
    except Exception as error:
        report.check(False, "Twilio account", f"{type(error).__name__}: {str(error)[:240]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    report = Report()
    check_runtime(report)
    settings = load_settings(report, args.env_file)
    if settings:
        check_storage(report, settings)
        check_aws(report, settings)
        check_twilio(report, settings)
    print(f"\nPreflight {'passed' if report.failures == 0 else 'failed'}: {report.failures} failure(s)")
    return 0 if report.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
