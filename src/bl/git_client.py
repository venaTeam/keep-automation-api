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
does not reliably cover DNS resolution, and on timeout the executor is torn down
*without waiting* so the caller is released immediately.

Threading: `python-gitlab` holds a non-thread-safe `requests.Session`, and the
BL runs in a threadpool, so a client is built fresh per call (see
`get_git_client`) rather than shared process-wide. Construction does no network
I/O — the project handle is lazy — so per-call creation is cheap.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Protocol, runtime_checkable

from src import config
from src.exceptions import ScriptRepoUnavailableError


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
            # lazy=True resolves no network round trip here — the project is a
            # proxy and the first real call carries the auth. A fresh Gitlab
            # client (and its requests.Session) is built per GitlabGitClient, so
            # nothing is shared across concurrent threadpool workers.
            self._project = gl.projects.get(config.GITLAB_SCRIPTS_PROJECT, lazy=True)
        return self._project

    @staticmethod
    def _bounded(fn, *args, **kwargs):
        """Run a blocking GitLab call under a hard wall-clock bound.

        Mirrors `ssrf._resolve_bounded`: `requests`' timeout misses DNS, so the
        call is wrapped in a single-use executor and reaped with
        `future.result(timeout)`. On timeout the executor is shut down with
        `wait=False` so the caller returns immediately instead of blocking until
        the hung request finally ends (the `with ThreadPoolExecutor(...)` form
        would block on exit via `shutdown(wait=True)`).
        """
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            return executor.submit(fn, *args, **kwargs).result(
                timeout=config.GITLAB_TIMEOUT_SECONDS
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

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
        # ("update") is the common case; a first commit (brand-new UUID path)
        # falls back to "create" — but ONLY when GitLab says the file is absent.
        # Any other error (protected branch, auth, 5xx, timeout) must NOT be
        # masked as a create; it surfaces as a retryable 503.
        try:
            try:
                commit = _commit("update")
            except gitlab.exceptions.GitlabCreateError as exc:
                if _is_missing_file(exc):
                    commit = _commit("create")
                else:
                    raise
            return commit.id
        except (gitlab.exceptions.GitlabError, FutureTimeoutError) as exc:
            raise ScriptRepoUnavailableError(
                f"GitLab commit failed for {script_path}"
            ) from exc

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
            raise ScriptRepoUnavailableError(
                f"GitLab read failed for {script_path}"
            ) from exc
        except (gitlab.exceptions.GitlabError, FutureTimeoutError) as exc:
            raise ScriptRepoUnavailableError(
                f"GitLab read failed for {script_path}"
            ) from exc
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _is_missing_file(exc) -> bool:
    """True only for the GitLab 400 that means 'this file does not exist yet'.

    GitLab returns 400 for several commit problems (protected branch, bad
    payload); only the missing-file case justifies retrying as a create.
    """
    message = getattr(exc, "error_message", "") or ""
    if not isinstance(message, str):
        message = str(message)
    message = message.lower()
    return getattr(exc, "response_code", None) == 400 and (
        "doesn't exist" in message or "does not exist" in message
    )


def build_gitlab_client() -> GitlabGitClient:
    """Construct the real client, or refuse / fail fast.

    Two guards, both raised on first request rather than at import:

    1. Refuse while user-tier auth is a noauth shim. The routes attribute every
       commit to the single service identity; with no real caller identity the
       human actor on the revision row is a placeholder and anyone who can reach
       `POST /automations` can write to the shared script repo. Do not mount the
       real client until user-tier auth is real (D14 safety gate).
    2. Fail fast if the script-repo config is missing — a deploy that forgets it
       must not silently fall back to the in-memory stub and lose every commit.
    """
    if config.AUTH_TYPE == "noauth":
        raise RuntimeError(
            "Refusing to serve the GitLab script-repo client while "
            "AUTH_TYPE=noauth: user-tier requests are unauthenticated, so every "
            "commit would be attributed to the service identity with no real "
            "caller recorded. Enable real user-tier auth first (D14 safety gate)."
        )
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
