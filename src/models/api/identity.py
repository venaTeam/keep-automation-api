"""The authenticated caller, as a schema rather than a bare dict.

`get_authenticated_entity` used to return `{"tenant_id": ..., "email": ...}`,
so every route indexed it by string key and a typo surfaced as a KeyError at
request time — on the path that decides **tenant scope**. Typing it moves that
to import time and gives the routes attribute access.

This is the seam the shared identity manager occupies later (§10.2 — never a
second auth stack). It must keep producing this shape: `tenant_id` is the only
source of tenant scope for the user tier, and the BL takes it as an explicit
argument precisely so no request body can supply it (spec §4.1/§8.1).
"""
from pydantic import BaseModel


class AuthenticatedEntity(BaseModel):
    """Who the request is running as. Server-derived, never client-supplied."""

    # Scopes match, author and read alike. A cross-tenant id is a 404, not a
    # 403, so existence never leaks (spec §8.1).
    tenant_id: str
    # Recorded as `created_by` / `updated_by` and on every revision row.
    email: str

    class Config:
        # The identity manager will hand over more claims than these two; the
        # extras are ignored rather than rejected so vendoring it is not a
        # breaking change here.
        extra = "ignore"
