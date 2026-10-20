"""CI webhook payload (automation-contracts.md §"CI webhook", spec §8.2).

Sent by the script repo's pipeline (F23) once per build of one automation's
path. Shape errors surface through the app-wide `RequestValidationError`
handler, so a malformed call gets the same `{errors:[{field,code,message}]}`
400 as authoring — a missing field is `field_required`, a malformed one
`invalid_value`.
"""
import re
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, validator
from pydantic.errors import MissingError

# Patterns are applied with `fullmatch` — `$` would also accept a trailing
# newline, which would then be stored verbatim as `active_digest`.
# Git object id: SHA-1 (40 hex) today; SHA-256 repositories (64 hex) accepted.
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# Digest-pinned image reference, `repository@sha256:<64 hex>`. Never a tag: the
# deployment must reference the exact bytes CI scanned (spec §5.1). The
# repository is a docker reference (host[:port]/path, lowercase); which
# repository is acceptable is checked against config in src/bl/builds.py.
_DIGEST_REF = re.compile(r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}")
CONTRACT_VERSION_MAX_LENGTH = 64


class BuildOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class CiWebhookIn(BaseModel):
    automation_id: UUID
    # The commit the pipeline built. Matched against `building_sha`, which is
    # what makes delivery idempotent per (automation, SHA).
    commit_sha: str
    status: BuildOutcome
    # Required on success, ignored on failure.
    image: str | None = None
    contract_version: str | None = None

    @validator("commit_sha")
    def commit_sha_is_git_object_id(cls, value: str) -> str:
        if not _COMMIT_SHA.fullmatch(value):
            raise ValueError("commit_sha must be a lowercase hex git object id")
        return value

    @validator("image", always=True)
    def image_is_digest_pinned(cls, value, values):
        if values.get("status") != BuildOutcome.SUCCESS:
            return value
        if value is None:
            raise MissingError()
        if not _DIGEST_REF.fullmatch(value):
            raise ValueError("image must be a digest-pinned reference (repo@sha256:<hex>)")
        return value

    @validator("contract_version", always=True)
    def contract_version_on_success(cls, value, values):
        if values.get("status") != BuildOutcome.SUCCESS:
            return value
        if value is None:
            raise MissingError()
        if not value.strip() or len(value) > CONTRACT_VERSION_MAX_LENGTH:
            raise ValueError(
                f"contract_version must be 1-{CONTRACT_VERSION_MAX_LENGTH} characters"
            )
        return value


class CiWebhookResult(str, Enum):
    # The build outcome was recorded (cutover or build_failed).
    APPLIED = "applied"
    # Nothing changed: a replay, a superseded SHA, or a deleting automation.
    IGNORED = "ignored"


class IgnoredReason(str, Enum):
    NOT_BUILDING = "not_building"
    SHA_MISMATCH = "sha_mismatch"
    DELETING = "deleting"


class CiWebhookOut(BaseModel):
    status: CiWebhookResult
    reason: IgnoredReason | None = None

    class Config:
        use_enum_values = True
