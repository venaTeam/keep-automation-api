"""User-tier authoring routes (stubs — logic lands in D13/D14/D18/D19).

Guarded by the existing identity provider (noauth shim for now).
"""
from fastapi import APIRouter, Depends

from src.api.deps import get_authenticated_entity

router = APIRouter(dependencies=[Depends(get_authenticated_entity)])


@router.get("/automations")
async def list_automations():
    return {"automations": []}


@router.get("/namespaces")
async def list_namespaces():
    return {"namespaces": []}


@router.get("/alert-schema/fields")
async def alert_schema_fields():
    return {"fields": []}
