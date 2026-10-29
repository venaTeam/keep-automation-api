"""Strict wire-shape validation based on caap.yaml; no onboarding policy."""

import json
from typing import Literal

from pydantic import (
    BaseModel,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    conint,
    conlist,
    root_validator,
    validator,
)

from pydantic.fields import SHAPE_LIST

from src.bl.capp_errors import CappError, CappErrorCode


class WireModel(BaseModel):
    @validator("*", pre=True)
    def wire_shapes(cls, value, field):
        if value is None:
            return value
        if field.shape == SHAPE_LIST:
            if not isinstance(value, list):
                raise ValueError("array required")
            if isinstance(field.type_, type) and issubclass(field.type_, BaseModel):
                if any(not isinstance(item, dict) for item in value):
                    raise ValueError("object required")
        elif isinstance(field.type_, type) and issubclass(field.type_, BaseModel):
            if not isinstance(value, dict):
                raise ValueError("object required")
        return value

    class Config:
        extra = "forbid"


class KeySelector(WireModel):
    name: StrictStr
    key: StrictStr


class EnvSource(WireModel):
    secretKeyRef: KeySelector | None = None
    configMapKeyRef: KeySelector | None = None

    @root_validator(skip_on_failure=True)
    def one_source(cls, values):
        if (
            sum(values.get(k) is not None for k in ("secretKeyRef", "configMapKeyRef"))
            != 1
        ):
            raise ValueError("one source required")
        return values


class EnvVar(WireModel):
    name: StrictStr
    value: StrictStr | None = None
    valueFrom: EnvSource | None = None

    @root_validator(skip_on_failure=True)
    def exclusive_value(cls, values):
        if values.get("value") is not None and values.get("valueFrom") is not None:
            raise ValueError("conflicting values")
        return values


class VolumeMount(WireModel):
    name: StrictStr
    mountPath: StrictStr


class SecretVolume(VolumeMount):
    secretName: StrictStr


class ConfigMapVolume(VolumeMount):
    configMapName: StrictStr


class NFSVolume(WireModel):
    name: StrictStr
    server: StrictStr
    path: StrictStr
    capacity: StrictStr


class Quantities(WireModel):
    cpu: StrictStr | None = None
    memory: StrictStr | None = None


class Resources(WireModel):
    requests: Quantities | None = None
    limits: Quantities | None = None


class Scale(WireModel):
    metric: Literal["rps", "concurrency", "cpu", "memory"] | None = None
    minReplicas: conint(strict=True, ge=0) | None = None
    maxReplicas: conint(strict=True, ge=1) | None = None
    scaleDelaySeconds: conint(strict=True, ge=0) | None = None


class Route(WireModel):
    hostname: StrictStr | None = None
    tlsEnabled: StrictBool | None = None
    routeTimeoutSeconds: StrictInt | None = None


class Log(WireModel):
    type: Literal["elastic", "elastic-datastream"] | None = None
    host: StrictStr | None = None
    index: StrictStr | None = None
    user: StrictStr | None = None
    passwordSecret: StrictStr | None = None
    passwordKey: StrictStr | None = None


class Ping(WireModel):
    schedule: StrictStr
    data: StrictStr | None = None


class Kafka(WireModel):
    bootstrapServers: conlist(StrictStr, min_items=1)
    topics: conlist(StrictStr, min_items=1)
    secretRef: StrictStr
    consumerGroup: StrictStr | None = None
    consumers: conint(strict=True, ge=1) | None = None


class Source(WireModel):
    name: StrictStr
    uri: StrictStr | None = None
    pingSourceConfiguration: Ping | None = None
    kafkaSourceConfiguration: Kafka | None = None

    @root_validator(skip_on_failure=True)
    def one_source(cls, values):
        if (
            sum(
                values.get(k) is not None
                for k in ("pingSourceConfiguration", "kafkaSourceConfiguration")
            )
            != 1
        ):
            raise ValueError("one source required")
        return values


class EventSources(WireModel):
    sources: list[Source] | None = None


class DeploymentConfig(WireModel):
    scaleSpec: Scale | None = None
    state: Literal["enabled"] | None = None
    containerName: StrictStr | None = None
    size: Literal["small", "medium", "large"] | None = None
    customResources: Resources | None = None
    env: list[EnvVar] | None = None
    volumeMounts: list[VolumeMount] | None = None
    routeSpec: Route | None = None
    logSpec: Log | None = None
    nfsVolumes: list[NFSVolume] | None = None
    secretVolumes: list[SecretVolume] | None = None
    configMapVolumes: list[ConfigMapVolume] | None = None
    eventSourcesSpec: EventSources | None = None


CONFIG_FIELDS = frozenset(DeploymentConfig.__fields__)


def encode_request(body: dict | None) -> bytes | None:
    """Never retain serialization errors, which can contain request secrets."""
    if body is None:
        return None
    try:
        return json.dumps(body, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        pass
    raise CappError(CappErrorCode.INVALID_REQUEST)


def validate_config(
    config: dict, *, error_code: CappErrorCode = CappErrorCode.INVALID_REQUEST
) -> None:
    # Validate original wire data, preserving omitted fields and explicit nulls.
    # Upstream settings use MALFORMED_RESPONSE; caller input uses INVALID_REQUEST.
    try:
        DeploymentConfig.parse_obj(config)
        if config.get("size") is not None and config.get("customResources") is not None:
            raise ValueError("conflicting sizing")
        return
    except (ValidationError, TypeError, ValueError, RecursionError):
        pass
    # Do not retain validation exceptions containing input or response values.
    raise CappError(error_code)
