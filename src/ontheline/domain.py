"""Validated inputs and outputs shared by the API and verification pipeline."""

from __future__ import annotations

from datetime import date, datetime
from enum import IntEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Tier(IntEnum):
    CONFIRMATION = 0
    SAFE_CHANGE = 1
    HUMAN_DECISION = 2
    SAFETY_STOP = 3


FieldName = Literal[
    "name",
    "description",
    "status",
    "schedule",
    "phone",
    "eligibility",
    "fees",
    "application_process",
    "alert",
    "address",
]


class ProposedFieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: FieldName
    new_value: Annotated[str, Field(min_length=1, max_length=2000)] | None
    quote: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    explicitly_confirmed: bool = False
    read_back_confirmed: bool = False
    removes_service: bool = False
    ambiguous: bool = False

class TranscriptTurn(BaseModel):
    role: Literal["assistant", "provider", "seeker", "system"]
    text: str = Field(min_length=1, max_length=4000)
    at: datetime | None = None


class ExtractionResult(BaseModel):
    """Schema Strands must produce before deterministic policy runs."""

    model_config = ConfigDict(extra="forbid")
    confirmed_fields: list[FieldName] = Field(default_factory=list, max_length=10)
    changes: list[ProposedFieldChange] = Field(default_factory=list, max_length=10)
    summary: str = Field(min_length=1, max_length=1000)
    outcome: Literal[
        "complete", "voicemail", "no_answer", "wrong_number", "stop_requested",
        "reached_client", "distress", "hostile", "ambiguous"
    ]
    disclosure_spoken: bool
    recording_consent: bool = False

    @model_validator(mode="after")
    def reject_conflicting_field_evidence(self):
        confirmed = set(self.confirmed_fields)
        changed = [change.field for change in self.changes]
        if len(confirmed) != len(self.confirmed_fields):
            raise ValueError("confirmed_fields cannot contain duplicates")
        if len(set(changed)) != len(changed):
            raise ValueError("changes cannot contain more than one proposal per field")
        overlap = confirmed.intersection(changed)
        if overlap:
            raise ValueError(f"fields cannot be both unchanged and changed: {', '.join(sorted(overlap))}")
        return self


class VerificationRequest(BaseModel):
    task_id: str
    transcript: list[TranscriptTurn] = Field(min_length=1, max_length=100)
    demo_scenario: Literal[
        "confirmed", "hours_changed", "program_closed", "uncertain_volunteer", "stop_calling"
    ] | None = None


class AgentCoreExtractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(
        min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    organization_name: str = Field(min_length=1, max_length=300)
    service_name: str = Field(min_length=1, max_length=300)
    fields: list[FieldName] = Field(min_length=1, max_length=10)
    transcript: list[TranscriptTurn] = Field(min_length=1, max_length=100)


class AgentCoreInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: AgentCoreExtractionInput


class DemoRequest(BaseModel):
    scenario: Literal[
        "confirmed", "hours_changed", "program_closed", "uncertain_volunteer", "stop_calling"
    ] = "confirmed"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1000)


class DecisionRequest(BaseModel):
    action: Literal["approve", "reject", "edit", "acknowledge"]
    edited_value: str | None = Field(default=None, max_length=2000)
    note: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def edit_requires_value(self):
        if self.action == "edit" and not self.edited_value:
            raise ValueError("edited_value is required when action is edit")
        return self


class IntakeSearch(BaseModel):
    need: str = Field(min_length=2, max_length=120)
    postal_code: str = Field(default="", max_length=20)
    limit: int = Field(default=5, ge=1, le=20)


class FailureReportRequest(BaseModel):
    service_id: str
    reason: Literal["disconnected", "closed", "wrong_hours", "ineligible", "moved", "other"]
    details: str = Field(default="", max_length=1000)
    referral_id: str | None = None


class ReferralAcceptRequest(BaseModel):
    service_id: str = Field(min_length=1, max_length=120)
    need: str = Field(min_length=2, max_length=120)
    postal_code: str = Field(default="", max_length=20)


class HSDSOrganizationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    email: str = Field(default="", max_length=320)
    website: str = Field(default="", max_length=2000)


class HSDSPhoneInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    number: str = Field(min_length=8, max_length=40)
    extension: str = Field(default="", max_length=20)
    type: str = Field(default="voice", max_length=40)
    description: str = Field(default="", max_length=500)


class HSDSScheduleInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    byday: str = Field(pattern=r"^(?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:MO|TU|WE|TH|FR|SA|SU))*$")
    opens_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    closes_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    valid_from: date | None = None
    valid_to: date | None = None
    description: str = Field(default="", max_length=500)


class HSDSAddressInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    address_1: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=200)
    region: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=30)


class HSDSLocationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    location_type: Literal["physical"] = "physical"
    name: str = Field(default="", max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    addresses: list[HSDSAddressInput] = Field(min_length=1, max_length=10)


class HSDSServiceAtLocationInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    location: HSDSLocationInput


class HSDSServiceInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    organization_id: UUID
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    status: Literal["active", "inactive", "defunct", "temporarily closed"]
    eligibility_description: str = Field(default="", max_length=4000)
    fees_description: str = Field(default="", max_length=2000)
    application_process: str = Field(default="", max_length=4000)
    alert: str = Field(default="", max_length=2000)
    organization: HSDSOrganizationInput
    phones: list[HSDSPhoneInput] = Field(default_factory=list, max_length=20)
    schedules: list[HSDSScheduleInput] = Field(default_factory=list, max_length=50)
    service_at_locations: list[HSDSServiceAtLocationInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def organization_ids_match(self):
        if self.organization_id != self.organization.id:
            raise ValueError("organization_id must match organization.id")
        return self


class HSDSImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    services: list[HSDSServiceInput] = Field(min_length=1, max_length=5000)


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    timezone: str = Field(min_length=1, max_length=100)
    allowed_days: list[Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]] = Field(
        min_length=1, max_length=7
    )
    earliest_local: str = Field(pattern=r"^\d{2}:\d{2}$")
    latest_local: str = Field(pattern=r"^\d{2}:\d{2}$")
    valid_from: date
    valid_until: date
    max_calls: int = Field(ge=1, le=10_000)
    recording_allowed: bool = False
    consented_by: str = Field(min_length=1, max_length=300)
    evidence_reference: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_window(self):
        if self.valid_until < self.valid_from:
            raise ValueError("valid_until must be on or after valid_from")
        for value in (self.earliest_local, self.latest_local):
            hour, minute = map(int, value.split(":"))
            if hour > 23 or minute > 59:
                raise ValueError("call window times must be valid 24-hour times")
        if len(set(self.allowed_days)) != len(self.allowed_days):
            raise ValueError("allowed_days cannot contain duplicates")
        return self
