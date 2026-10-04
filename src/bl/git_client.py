"""Script-repo client seam.

Git is authoritative for script bytes — the DB stores only `script_path`
(`{automation_id}/script.py`), never the source (spec §5.1). D13 shipped the
interface plus an in-memory stand-in so authoring CRUD is fully testable; D14
lands the real GitLab implementation (`GitlabGitClient`) behind `get_git_client`
without touching callers. The stub stays as the test double (injected via
`app.dependency_overrides`); production fails fast if GitLab is unconfigured.

Blocking-I/O pattern (pinned for this service, same as `src/bl/ssrf.py`): the BL
is synchronous and routes run it via `run_in_threadpool`, so a stalled syscall
never blocks the event loop; every network call is additionally bounded by a
single-use executor + `future.result(timeout)` because `requests`' own timeout
does not reliably cover DNS resolution.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol, runtime_checkable

from src import config


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


class GitlabGitClient:
    """Real script repo over the GitLab REST API (python-gitlab).

    Stateless — no local checkout, no git binary — so it is safe across the
    service's gunicorn workers. The GitLab client and project handle are built
    lazily on first use: constructing this object does no network I/O, so
    `build_gitlab_client`'s fail-fast config check stays synchronous.

    Commits are authored by the single service identity (`config.GITLAB_COMMIT_*`
    + the token's own identity); the human actor is recorded only on the
    `automation_revisions` row by the BL, never as the git author (§4.4).
    """

    def __init__(self, project=None):
        # `project` is injected by tests with a fake; production leaves it None
        # and resolves it lazily against GitLab.
        self._project = project

    def _get_project(self):
        if self._project is None:
            import gitlab

            gl = gitlab.Gitlab(
                config.GITLAB_URL,
                private_token=config.GITLAB_SCRIPTS_TOKEN,
                timeout=config.GITLAB_TIMEOUT_SECONDS,
            )
            self._project = self._bounded(gl.projects.get, config.GITLAB_SCRIPTS_PROJECT)
        return self._project

    @staticmethod
    def _bounded(fn, *args, **kwargs):
        """Run a blocking GitLab call under a hard wall-clock bound.

        Mirrors `ssrf._resolve_bounded`: `requests`' timeout misses DNS, so the
        call is wrapped in a single-use executor and reaped with
        `future.result(timeout)`. A `FutureTimeoutError` propagates so the BL
        transaction rolls back (commit) or the GET surfaces an error (read).
        """
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fn, *args, **kwargs)
            return future.result(timeout=config.GITLAB_TIMEOUT_SECONDS)

    def commit_script(self, script_path: str, content: str, message: str) -> str:
        import gitlab

        project = self._get_project()

        def _commit(action: str):
            return self._bounded(
                project.commits.create,
                {
                    "branch": config.GITLAB_SCRIPTS_BRANCH,
                    "commit_message": message,
                    "actions": [
                        {"action": action, "file_path": script_path, "content": content}
                    ],
                    "author_name": config.GITLAB_COMMIT_AUTHOR_NAME,
                    "author_email": config.GITLAB_COMMIT_AUTHOR_EMAIL,
                },
            )

        # The Protocol hides create-vs-update, so resolve it here: an edit
        # ("update") is the common case; a first commit (brand-new UUID path, or
        # a retry after a prior success that failed to persist its DB row) falls
        # back to "create". Any other GitLab error propagates — "raises on
        # failure" is the Protocol contract the BL relies on to roll back.
        try:
            commit = _commit("update")
        except gitlab.exceptions.GitlabCreateError as exc:
            if exc.response_code == 400:
                commit = _commit("create")
            else:
                raise
        return commit.id

    def read_script(self, script_path: str) -> str | None:
        import gitlab

        project = self._get_project()
        try:
            raw = self._bounded(
                project.files.raw,
                file_path=script_path,
                ref=config.GITLAB_SCRIPTS_BRANCH,
            )
        except gitlab.exceptions.GitlabGetError as exc:
            if exc.response_code == 404:
                return None
            raise
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def build_gitlab_client() -> GitlabGitClient:
    """Construct the real client, or fail fast if GitLab is unconfigured.

    A deploy that forgets the script-repo config must not silently fall back to
    the in-memory stub and lose every commit — so a missing URL / token /
    project is a hard error here (raised on first request, not at import).
    """
    missing = [
        name
        for name, value in (
            ("GITLAB_URL", config.GITLAB_URL),
            ("GITLAB_SCRIPTS_TOKEN", config.GITLAB_SCRIPTS_TOKEN),
            ("GITLAB_SCRIPTS_PROJECT", config.GITLAB_SCRIPTS_PROJECT),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "GitLab script-repo is not configured: set "
            + ", ".join(missing)
            + " (spec §5.1). Tests inject an in-memory client via "
            "app.dependency_overrides."
        )
    return GitlabGitClient()
