"""Script-repo client seam (D14 lands the real GitLab implementation).

Git is authoritative for script bytes — the DB stores only `script_path`
(`{automation_id}/script.py`), never the source (spec §5.1). D13 ships the
interface plus an in-memory stand-in so authoring CRUD is fully testable;
D14 swaps the implementation behind `get_git_client` without touching callers.

Blocking-I/O pattern for the real implementation: the BL is synchronous and
routes run it via `run_in_threadpool`; bound any network call with an explicit
timeout (same pattern as `src/bl/ssrf.py`).
"""
import hashlib
import threading
from typing import Protocol, runtime_checkable
from uuid import UUID

# Delete = archive-mark, never erase (spec §5.4). One deterministic marker file
# per automation path; F23's CI must ignore commits that only touch it (C3), or
# deleting an automation would start a fresh image build.
ARCHIVE_MARKER = ".archived"


def archive_marker_path(automation_id: UUID | str) -> str:
    return f"{automation_id}/{ARCHIVE_MARKER}"


@runtime_checkable
class GitClient(Protocol):
    def commit_script(self, script_path: str, content: str, message: str) -> str:
        """Commit script bytes; return the commit SHA. Raises on failure."""
        ...

    def read_script(self, script_path: str) -> str | None:
        """Committed bytes at path, or None if absent."""
        ...

    def archive(self, automation_id: UUID, actor: str) -> str:
        """Archive-mark the automation's path (D18 cascade step 4; real one: D14).

        Idempotent: if the marker already exists, make no commit and return the
        SHA that created it. Script bytes and history stay in place.
        """
        ...


class InMemoryGitClient:
    """Deterministic stand-in: sha1 over path + revision counter + content."""

    def __init__(self):
        self._files: dict[str, str] = {}
        self._revisions: dict[str, int] = {}
        self._shas: dict[str, str] = {}
        # Re-entrant: `archive` holds it across its check-and-commit.
        self._lock = threading.RLock()
        # Every commit made, in order — lets tests assert "no duplicate archive".
        self.commit_log: list[str] = []

    def commit_script(self, script_path: str, content: str, message: str) -> str:
        with self._lock:
            revision = self._revisions.get(script_path, 0) + 1
            self._revisions[script_path] = revision
            self._files[script_path] = content
            digest = hashlib.sha1(
                f"{script_path}@{revision}\n{content}".encode("utf-8")
            ).hexdigest()
            self._shas[script_path] = digest
            self.commit_log.append(message)
            return digest

    def read_script(self, script_path: str) -> str | None:
        return self._files.get(script_path)

    def archive(self, automation_id: UUID, actor: str) -> str:
        path = archive_marker_path(automation_id)
        with self._lock:
            if path in self._files:
                return self._shas[path]
            return self.commit_script(
                path, f"archived by {actor}\n", f"archive {automation_id}"
            )


# Process-wide stub instance (replaced by the real client in D14; tests inject
# their own via app.dependency_overrides).
_default_client = InMemoryGitClient()


def get_default_git_client() -> InMemoryGitClient:
    return _default_client
