"""Internal adapter inputs, never author-facing API request models."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ConditionStatus(StrEnum):
    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


class ConditionSource(StrEnum):
    CAPP = "capp"
    KNATIVE = "knative"
    LOGGING = "logging"
    ROUTE_DOMAIN_MAPPING = "route.domainmapping"


@dataclass(frozen=True)
class CappWalletTarget:
    cluster: str
    wallet_name: str


@dataclass(frozen=True)
class CappRequest:
    name: str
    image: str
    # CAPP wire field names; omitted fields are preserved on update. Lists
    # replace in full, dictionaries merge recursively (including explicit null).
    config: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SecretRequest:
    name: str
    data: dict[str, str] = field(repr=False)
    type: str = "Opaque"


@dataclass(frozen=True)
class SecretUpdateRequest:
    data: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class CappCondition:
    type: str
    status: ConditionStatus
    source: ConditionSource | None = None


@dataclass(frozen=True)
class CappResource:
    name: str
    uid: str
    deployment_url: str | None
    image: str
    conditions: tuple[CappCondition, ...]
    resource_version: str | None = None


@dataclass(frozen=True)
class SecretResource:
    name: str
    namespace: str
    uid: str
    resource_version: str | None = None
