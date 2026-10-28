"""Validate CAPP responses and expose only supported resource fields."""

import re
from typing import Any

from src.bl.capp_errors import CappError, CappErrorCode
from src.bl.capp_models import (
    CappCondition,
    CappResource,
    SecretResource,
    ConditionSource,
    ConditionStatus,
)

JsonObject = dict[str, Any]
_CONDITION_STATUSES = frozenset(ConditionStatus)
_CONDITION_SOURCES = frozenset(ConditionSource)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _optional_object(payload: JsonObject, key: str) -> JsonObject:
    value = payload.get(key)
    return {} if value is None else _object(value)


def required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _optional_string(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return value


def _conditions(payload: JsonObject) -> tuple[CappCondition, ...]:
    status = _optional_object(payload, "status")
    items = status.get("conditions", [])
    if not isinstance(items, list):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    conditions = []
    for item in items:
        item = _object(item)
        condition_type = required_string(item, "type")
        condition_status = required_string(item, "status")
        source = _optional_string(item, "source")
        if condition_status not in _CONDITION_STATUSES:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        if source is not None and source not in _CONDITION_SOURCES:
            raise CappError(CappErrorCode.MALFORMED_RESPONSE)
        # Freeform reason/message can echo secrets. Deliberately omit them.
        conditions.append(
            CappCondition(
                type=condition_type,
                status=ConditionStatus(condition_status),
                source=ConditionSource(source) if source is not None else None,
            )
        )
    return tuple(conditions)


def _deployment_url(payload: JsonObject) -> str | None:
    route = _optional_object(payload, "routeSpec")
    hostname = _optional_string(route, "hostname")
    if not hostname:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", hostname):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    tls_enabled = route.get("tlsEnabled", False)
    if not isinstance(tls_enabled, bool):
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    scheme = "https" if tls_enabled else "http"
    return f"{scheme}://{hostname}"


def parse_capp(
    raw: JsonObject,
    namespace: str,
    name: str,
) -> CappResource:
    if raw.get("name") != name or raw.get("namespace", namespace) != namespace:
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    return CappResource(
        name=name,
        uid=required_string(raw, "uid"),
        deployment_url=_deployment_url(raw),
        image=required_string(raw, "image"),
        conditions=_conditions(raw),
        resource_version=_optional_string(raw, "resourceVersion"),
    )


def parse_secret(
    raw: JsonObject,
    namespace: str,
    name: str,
) -> SecretResource:
    if raw.get("name") != name or raw.get("namespace") != namespace:
        raise CappError(CappErrorCode.MALFORMED_RESPONSE)
    # Never return Secret data or arbitrary labels/annotations to callers.
    return SecretResource(
        name=name,
        namespace=namespace,
        uid=required_string(raw, "uid"),
        resource_version=_optional_string(raw, "resourceVersion"),
    )
