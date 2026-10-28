"""CappClient contract tests, run against every implementation.

The CI-webhook tests drive `InMemoryCappClient`, so on their own they pin the
contract on the double only. These tests bind any client: D16 adds its real
client here (against a CAPP fake or sandbox) with probes that read where a
deployment actually runs and what it serves. Contract: the `CappClient`
docstring and automation-contracts.md §"CAPP integration".
"""
from uuid import uuid4

import pytest

from src.bl.capp_client import InMemoryCappClient

# (factory, deployed_wallet(client, automation_id), served_image(client, deployment_id))
SUBJECTS = [
    pytest.param(
        InMemoryCappClient,
        lambda capp, automation_id: capp.wallets.get(str(automation_id)),
        lambda capp, deployment_id: capp.images.get(deployment_id),
        id="in-memory",
    ),
]


@pytest.mark.parametrize("factory, deployed_wallet, served_image", SUBJECTS)
def test_create_is_idempotent_by_automation_id(factory, deployed_wallet, served_image):
    capp = factory()
    automation_id = uuid4()
    first = capp.create_deployment(
        automation_id=automation_id, wallet="old-wallet", image="i1", secret_refs=[]
    )
    again = capp.create_deployment(
        automation_id=automation_id, wallet="old-wallet", image="i2", secret_refs=[]
    )
    assert again == first
    assert deployed_wallet(capp, automation_id) == "old-wallet"
    assert served_image(capp, first.deployment_id) == "i2"


@pytest.mark.parametrize("factory, deployed_wallet, served_image", SUBJECTS)
def test_create_never_reuses_a_deployment_from_another_wallet(
    factory, deployed_wallet, served_image
):
    # Reachable via the CAPP-ahead-of-row drift: an unstored create, then a
    # wallet edit before the next build. Returning the old-wallet deployment
    # would run the automation on the wrong wallet's budget and secrets.
    capp = factory()
    automation_id = uuid4()
    first = capp.create_deployment(
        automation_id=automation_id, wallet="old-wallet", image="i1", secret_refs=[]
    )
    moved = capp.create_deployment(
        automation_id=automation_id, wallet="new-wallet", image="i3", secret_refs=[]
    )
    assert deployed_wallet(capp, automation_id) == "new-wallet"
    assert served_image(capp, moved.deployment_id) == "i3"
    if moved.deployment_id != first.deployment_id:
        # The old-wallet deployment was torn down, not left serving.
        assert served_image(capp, first.deployment_id) is None


@pytest.mark.parametrize("factory, deployed_wallet, served_image", SUBJECTS)
def test_rollout_serves_the_new_image_in_place(factory, deployed_wallet, served_image):
    capp = factory()
    automation_id = uuid4()
    deployment = capp.create_deployment(
        automation_id=automation_id, wallet="w", image="i1", secret_refs=["s1"]
    )
    capp.rollout(deployment_id=deployment.deployment_id, image="i2", secret_refs=["s2"])
    assert served_image(capp, deployment.deployment_id) == "i2"
    assert deployed_wallet(capp, automation_id) == "w"
