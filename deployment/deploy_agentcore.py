"""Create an Amazon Bedrock AgentCore Runtime from a published ARM64 image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from typing import Any

import boto3

TERMINAL_FAILURE_STATES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}
DEFAULT_LIFECYCLE = {"idleRuntimeSessionTimeout": 300, "maxLifetime": 3600}


def environment(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError("environment values must use NAME=VALUE")
        key, item = value.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise argparse.ArgumentTypeError(f"invalid environment variable name: {key}")
        result[key] = item
    return result


def wait_until_ready(
    client: Any,
    runtime_id: str,
    *,
    timeout_seconds: int = 900,
    poll_seconds: float = 5.0,
    expected_version: str | None = None,
    require_mmdsv2: bool = False,
) -> dict[str, Any]:
    """Wait for an AgentCore create/update operation and surface its failure reason."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = runtime.get("status", "UNKNOWN")
        version_matches = expected_version is None or runtime.get("agentRuntimeVersion") == expected_version
        metadata_ready = (
            not require_mmdsv2
            or (runtime.get("metadataConfiguration") or {}).get("requireMMDSV2") is True
        )
        if status == "READY" and version_matches and metadata_ready:
            return runtime
        if status in TERMINAL_FAILURE_STATES:
            reason = runtime.get("failureReason") or "no failure reason was returned"
            raise RuntimeError(f"AgentCore runtime entered {status}: {reason}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"AgentCore runtime did not become READY within {timeout_seconds} seconds "
                f"(last status: {status})"
            )
        time.sleep(poll_seconds)


def enable_mmdsv2(
    client: Any,
    *,
    runtime_id: str,
    image_uri: str,
    role_arn: str,
    description: str,
    runtime_environment: dict[str, str],
) -> dict[str, Any]:
    """Apply the MMDSv2 setting required for every invocation after 30 June 2026.

    AgentCore currently exposes ``metadataConfiguration`` on UpdateAgentRuntime but not on
    CreateAgentRuntime, so a new container runtime needs this immediate second operation.
    """
    return client.update_agent_runtime(
        agentRuntimeId=runtime_id,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": image_uri}},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration=DEFAULT_LIFECYCLE,
        metadataConfiguration={"requireMMDSV2": True},
        environmentVariables=runtime_environment,
        description=description,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--description", default="On the Line community directory verification agent")
    parser.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=900,
        help="seconds to wait for each create/update operation (default: 900)",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", args.name):
        parser.error("--name must match [A-Za-z][A-Za-z0-9_]{0,47}")
    if ".dkr.ecr." not in args.image_uri or "amazonaws.com/" not in args.image_uri:
        parser.error("--image-uri must identify an Amazon ECR image")
    if args.wait_timeout < 60:
        parser.error("--wait-timeout must be at least 60 seconds")
    runtime_environment = environment(args.env)
    runtime_environment.setdefault("OTL_ENVIRONMENT", "agentcore")
    runtime_environment.setdefault("OTL_AGENT_MODE", "bedrock")
    runtime_environment.setdefault("OTL_AWS_REGION", args.region)
    fingerprint = json.dumps(
        [args.name, args.image_uri, args.role_arn, args.region, runtime_environment],
        sort_keys=True,
    )
    client = boto3.client("bedrock-agentcore-control", region_name=args.region)
    response = client.create_agent_runtime(
        agentRuntimeName=args.name,
        agentRuntimeArtifact={"containerConfiguration": {"containerUri": args.image_uri}},
        roleArn=args.role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": "HTTP"},
        lifecycleConfiguration=DEFAULT_LIFECYCLE,
        environmentVariables=runtime_environment,
        clientToken=hashlib.sha256(fingerprint.encode()).hexdigest(),
        description=args.description,
        tags={"project": "ontheline", "managed-by": "deployment-script"},
    )
    created = wait_until_ready(
        client,
        response["agentRuntimeId"],
        timeout_seconds=args.wait_timeout,
        expected_version=response.get("agentRuntimeVersion"),
    )
    update = enable_mmdsv2(
        client,
        runtime_id=response["agentRuntimeId"],
        image_uri=args.image_uri,
        role_arn=args.role_arn,
        description=args.description,
        runtime_environment=runtime_environment,
    )
    ready = wait_until_ready(
        client,
        response["agentRuntimeId"],
        timeout_seconds=args.wait_timeout,
        expected_version=update.get("agentRuntimeVersion"),
        require_mmdsv2=True,
    )
    print(json.dumps({
        "agentRuntimeArn": response["agentRuntimeArn"],
        "agentRuntimeId": response["agentRuntimeId"],
        "agentRuntimeVersion": ready.get("agentRuntimeVersion"),
        "status": ready["status"],
        "requireMMDSV2": ready.get("metadataConfiguration", {}).get("requireMMDSV2"),
        "createdVersion": created.get("agentRuntimeVersion"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
