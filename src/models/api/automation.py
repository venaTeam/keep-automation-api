"""Authoring request/response models (Pydantic v1 — `.dict()`, `validator`, `Config`).

Shape only: cross-field rules (allowlist, min-2 triggers, cooldown coupling,
SSRF, AST) live in `src/bl/validation.py` so failures ACCUMULATE into one
machine-readable 400 instead of failing fast at the model boundary. The only
model-boundary guard is the script byte cap (CPU/memory protection — reject
oversized payloads before any parsing work).
"""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, validator

from src.contracts.limits import SCRIPT_MAX_BYTES
from src.models.db.automation import BuildState, MatchingState


class AutomationIn(BaseModel):
    """Create/edit payload (spec §4.1)."""

    name: str
    namespace: str
    script: str
    triggers: list
    secret_name: str | None = None
    cooldown_seconds: int | None = None
    cooldown_fields: list | None = None
    timeout_seconds: int | None = None
    grace_seconds: int | None = None
    logstash_url: str | None = None

    @validator("script")
    def script_within_byte_cap(cls, value: str) -> str:
        if len(value.encode("utf-8")) > SCRIPT_MAX_BYTES:
            raise ValueError(f"script exceeds {SCRIPT_MAX_BYTES} bytes")
        return value


class AutomationListItem(BaseModel):
    """List row — no script body (spec §8.1)."""

    id: UUID
    name: str
    namespace: str
    triggers: list
    matching_state: MatchingState
    build_state: BuildState
    active_digest: str | None
    contract_version: str | None
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
        use_enum_values = True


class AutomationOut(AutomationListItem):
    """Detail — full definition; script is read back from git (D14 interface)."""

    script: str | None = None
    secret_name: str | None
    cooldown_seconds: int | None
    cooldown_fields: list | None
    timeout_seconds: int | None
    grace_seconds: int
    logstash_url: str | None
