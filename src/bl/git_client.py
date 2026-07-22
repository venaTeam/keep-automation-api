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
from typing import Protocol, runtime_checkable


@runtime_checkable
class GitClient(Protocol):
    def commit_script(self, script_path: str, content: str, message: str) -> str:
        """Commit script bytes; return the commit SHA. Raises on failure."""
        ...

    def read_script(self, script_path: str) -> str | None:
        """Committed bytes at path, or None if absent."""
        ...


class InMemoryGitClient:
    """Deterministic stand-in: sha1 over path + revision counter + content."""

    def __init__(self):
        self._files: dict[str, str] = {}
        self._revisions: dict[str, int] = {}

    def commit_script(self, script_path: str, content: str, message: str) -> str:
        revision = self._revisions.get(script_path, 0) + 1
        self._revisions[script_path] = revision
        self._files[script_path] = content
        digest = hashlib.sha1(
            f"{script_path}@{revision}\n{content}".encode("utf-8")
        ).hexdigest()
        return digest

    def read_script(self, script_path: str) -> str | None:
        return self._files.get(script_path)


# Process-wide stub instance (replaced by the real client in D14; tests inject
# their own via app.dependency_overrides).
_default_client = InMemoryGitClient()


def get_default_git_client() -> InMemoryGitClient:
    return _default_client
