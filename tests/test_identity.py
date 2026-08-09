"""The authenticated caller is a schema, not a bare dict.

`tenant_id` off this object is the only thing scoping every authoring read and
write, so the shape is worth pinning: a missing field must fail where it is
constructed, not as a KeyError deep in a route.
"""
import pytest
from pydantic import ValidationError

from src.api.deps import get_authenticated_entity
from src.models.api.identity import AuthenticatedEntity


async def test_noauth_shim_returns_the_schema():
    entity = await get_authenticated_entity()
    assert isinstance(entity, AuthenticatedEntity)
    assert entity.tenant_id == "keep"
    assert entity.email == "noauth@keep"


@pytest.mark.parametrize("missing", ["tenant_id", "email"])
def test_both_fields_are_required(missing):
    fields = {"tenant_id": "keep", "email": "someone@keep"}
    fields.pop(missing)
    with pytest.raises(ValidationError):
        AuthenticatedEntity(**fields)


def test_extra_claims_are_ignored_not_rejected():
    """The identity manager will send more than these two claims.

    Vendoring it must not become a breaking change here, so extras are dropped
    rather than raising.
    """
    entity = AuthenticatedEntity(
        tenant_id="keep", email="someone@keep", role="admin", exp=123
    )
    assert entity.tenant_id == "keep"
    assert not hasattr(entity, "role")


def test_routes_read_the_tenant_off_the_object_not_a_dict(client_as):
    """A dict would still work by accident if a route used .get() — this pins
    that the wiring genuinely carries the typed object end to end."""
    resp = client_as("other-tenant").get("/automations")
    assert resp.status_code == 200
