"""In-memory cascade adapters with a shared world + call journal.

`World` is the "external systems" state that survives a runner crash: a new
set of fakes built over the same World is a fresh process talking to the same
CAPP / registry / git. Every call is appended to `world.calls` so tests can
assert exact cross-adapter ordering.
"""
from dataclasses import dataclass, field
from uuid import UUID

from src.bl.cascade import CascadeDeps
from src.bl.git_client import InMemoryGitClient, archive_marker_path


class SimulatedFailure(Exception):
    """An injected adapter failure (timeout / 401 / 403 / 5xx / crash)."""


@dataclass
class World:
    capps: set = field(default_factory=set)  # {(wallet, name)}
    secrets: set = field(default_factory=set)  # {(wallet, name)}
    images: dict = field(default_factory=dict)  # {automation_id: set(digest)}
    git: InMemoryGitClient = field(default_factory=InMemoryGitClient)
    calls: list = field(default_factory=list)
    # {operation: remaining failure count}; -1 = fail forever.
    failures: dict = field(default_factory=dict)
    publishes: int = 0

    def maybe_fail(self, operation: str) -> None:
        remaining = self.failures.get(operation, 0)
        if remaining == 0:
            return
        if remaining > 0:
            self.failures[operation] = remaining - 1
        raise SimulatedFailure(f"{operation} failed: token=super-secret-value")

    def operations(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeCapp:
    def __init__(self, world: World):
        self.world = world

    def delete_capp(self, wallet: str, name: str) -> None:
        self.world.calls.append(("delete_capp", wallet, name))
        self.world.maybe_fail("delete_capp")
        self.world.capps.discard((wallet, name))  # absent = 404 = success
        self.world.maybe_fail("after_delete_capp")

    def delete_secret(self, wallet: str, name: str) -> None:
        self.world.calls.append(("delete_secret", wallet, name))
        self.world.maybe_fail("delete_secret")
        self.world.secrets.discard((wallet, name))
        self.world.maybe_fail("after_delete_secret")


class FakeRegistry:
    def __init__(self, world: World):
        self.world = world

    def list_image_digests(self, automation_id: UUID) -> list[str]:
        self.world.calls.append(("list_images", str(automation_id)))
        self.world.maybe_fail("list_images")
        return sorted(self.world.images.get(str(automation_id), set()))

    def delete_image(self, automation_id: UUID, digest: str) -> None:
        self.world.calls.append(("delete_image", str(automation_id), digest))
        self.world.maybe_fail("delete_image")
        self.world.images.get(str(automation_id), set()).discard(digest)


class FakeGit:
    def __init__(self, world: World):
        self.world = world

    def commit_script(self, script_path: str, content: str, message: str) -> str:
        return self.world.git.commit_script(script_path, content, message)

    def read_script(self, script_path: str) -> str | None:
        return self.world.git.read_script(script_path)

    def archive(self, automation_id: UUID, actor: str) -> str:
        self.world.calls.append(("archive", str(automation_id), actor))
        self.world.maybe_fail("archive")
        sha = self.world.git.archive(automation_id, actor)
        self.world.maybe_fail("after_archive")
        return sha

    def archived(self, automation_id) -> bool:
        return self.world.git.read_script(archive_marker_path(automation_id)) is not None

    def archive_commits(self, automation_id) -> int:
        return self.world.git.commit_log.count(f"archive {automation_id}")


class FakePublisher:
    def __init__(self, world: World):
        self.world = world

    def publish_reload(self) -> None:
        self.world.calls.append(("publish_reload",))
        self.world.publishes += 1


def deps_for(world: World) -> CascadeDeps:
    """A fresh runner's adapters over the shared external state."""
    return CascadeDeps(
        capp=FakeCapp(world),
        registry=FakeRegistry(world),
        git=FakeGit(world),
        publisher=FakePublisher(world),
    )


def seed_external(world: World, automation_id, wallet: str, capp_name: str | None = None, digests=("sha256:1", "sha256:2")) -> None:
    """Resources a successful deploy would have left behind."""
    world.capps.add((wallet, capp_name or f"automation-{automation_id}"))
    world.secrets.add((wallet, f"automation-{automation_id}-run-auth"))
    world.images[str(automation_id)] = set(digests)
