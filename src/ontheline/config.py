"""Runtime configuration with fail-closed production defaults."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OTL_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production", "agentcore"] = "development"
    database_path: Path = Path("data/ontheline.db")
    agent_session_path: Path = Path("data/strands-sessions")
    secret_key: str = Field(default="development-only-change-me", min_length=20)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    forwarded_allow_ips: str = "127.0.0.1"
    https_only: bool = False
    agent_mode: Literal["demo", "bedrock", "agentcore"] = "demo"
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "us.amazon.nova-2-lite-v1:0"
    public_base_url: str = "http://localhost:8000"
    telephony_provider: Literal["disabled", "twilio"] = "disabled"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    bootstrap_sample_data: bool = True
    agentcore_runtime_arn: str = ""
    directory_name: str = "On the Line Community Directory"
    minimum_call_interval_days: int = Field(default=1, ge=1, le=30)
    session_hours: int = Field(default=12, ge=1, le=168)
    login_attempts_per_minute: int = Field(default=8, ge=1, le=100)
    curator_username: str = "judge"
    curator_password: str = "ontheline-demo-2026"

    @model_validator(mode="after")
    def reject_insecure_production_key(self):
        if self.telephony_provider == "twilio":
            required = {
                "OTL_TWILIO_ACCOUNT_SID": self.twilio_account_sid,
                "OTL_TWILIO_AUTH_TOKEN": self.twilio_auth_token,
                "OTL_TWILIO_FROM_NUMBER": self.twilio_from_number,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"Twilio telephony requires: {', '.join(missing)}")
        if self.agent_mode == "agentcore" and not self.agentcore_runtime_arn:
            raise ValueError("OTL_AGENTCORE_RUNTIME_ARN is required in agentcore mode")
        if self.environment == "agentcore" and self.agent_mode != "bedrock":
            raise ValueError("AgentCore extraction runtime requires OTL_AGENT_MODE=bedrock")
        if self.environment == "production":
            errors = []
            if self.secret_key == "development-only-change-me" or len(self.secret_key) < 32:
                errors.append("OTL_SECRET_KEY must be a unique value of at least 32 characters")
            if self.curator_password == "ontheline-demo-2026" or len(self.curator_password) < 14:
                errors.append(
                    "OTL_CURATOR_PASSWORD must be changed and contain at least 14 characters"
                )
            if self.agent_mode not in {"bedrock", "agentcore"}:
                errors.append("OTL_AGENT_MODE must be bedrock or agentcore")
            if self.telephony_provider != "twilio":
                errors.append("OTL_TELEPHONY_PROVIDER must be twilio")
            production_secrets = {
                "OTL_SECRET_KEY": self.secret_key,
                "OTL_CURATOR_PASSWORD": self.curator_password,
                "OTL_TWILIO_ACCOUNT_SID": self.twilio_account_sid,
                "OTL_TWILIO_AUTH_TOKEN": self.twilio_auth_token,
                "OTL_TWILIO_FROM_NUMBER": self.twilio_from_number,
                "OTL_AGENTCORE_RUNTIME_ARN": (
                    self.agentcore_runtime_arn if self.agent_mode == "agentcore" else ""
                ),
            }
            placeholders = [
                name
                for name, value in production_secrets.items()
                if value and any(marker in value.upper() for marker in ("CHANGE_ME", "REPLACE_ME"))
            ]
            if placeholders:
                errors.append(f"placeholder values must be replaced: {', '.join(placeholders)}")
            if self.bootstrap_sample_data:
                errors.append("OTL_BOOTSTRAP_SAMPLE_DATA must be false")
            if not self.https_only:
                errors.append("OTL_HTTPS_ONLY must be true")
            if urlparse(self.public_base_url).scheme != "https":
                errors.append("OTL_PUBLIC_BASE_URL must use https")
            if not self.forwarded_allow_ips.strip():
                errors.append("OTL_FORWARDED_ALLOW_IPS must identify the trusted proxy")
            if {"*", "testserver"}.intersection(self.host_list):
                errors.append("OTL_ALLOWED_HOSTS must contain only deployed hostnames")
            if errors:
                raise ValueError("; ".join(errors))
        return self

    @property
    def host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]


def password_hash(password: str, *, salt: bytes | None = None) -> str:
    """Return a versioned scrypt hash suitable for storage."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
            return False
        if len(salt) != 32 or len(expected) != 64:
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return secrets.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False
