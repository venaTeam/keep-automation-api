"""InMemoryGitClient behavior + protocol conformance, and the real
GitlabGitClient mapped against a fake python-gitlab project (no network)."""
import time

import gitlab
import pytest

from src import config
from src.bl.git_client import (
    GitClient,
    GitlabGitClient,
    InMemoryGitClient,
    build_gitlab_client,
)
from src.exceptions import ScriptRepoUnavailableError


def test_commit_returns_sha_and_read_back_is_byte_identical():
    client = InMemoryGitClient()
    sha = client.commit_script("abc/script.py", "def handle(alert): pass", "create")
    assert isinstance(sha, str) and len(sha) == 40
    assert client.read_script("abc/script.py") == "def handle(alert): pass"


def test_recommit_changes_sha_and_content():
    client = InMemoryGitClient()
    first = client.commit_script("abc/script.py", "v1", "create")
    second = client.commit_script("abc/script.py", "v2", "edit")
    assert first != second
    assert client.read_script("abc/script.py") == "v2"


def test_read_missing_path_returns_none():
    assert InMemoryGitClient().read_script("nope/script.py") is None


def test_stub_satisfies_protocol():
    assert isinstance(InMemoryGitClient(), GitClient)


# --- GitlabGitClient against a fake python-gitlab project ------------------


class FakeCommit:
    def __init__(self, sha):
        self.id = sha


class FakeCommits:
    def __init__(self, missing_on_update=False):
        self.missing_on_update = missing_on_update
        self.calls = []

    def create(self, payload):
        action = payload["actions"][0]["action"]
        self.calls.append(payload)
        if action == "update" and self.missing_on_update:
            raise gitlab.exceptions.GitlabCreateError(
                "A file with this name doesn't exist", response_code=400
            )
        return FakeCommit("deadbeef" * 5)


class FakeFiles:
    def __init__(self, content=None, error_code=None, slow_seconds=0.0):
        self.content = content
        self.error_code = error_code
        self.slow_seconds = slow_seconds

    def raw(self, file_path, ref):
        if self.slow_seconds:
            time.sleep(self.slow_seconds)
        if self.error_code is not None:
            raise gitlab.exceptions.GitlabGetError(
                "boom", response_code=self.error_code
            )
        return self.content


class FakeProject:
    def __init__(self, commits=None, files=None):
        self.commits = commits or FakeCommits()
        self.files = files or FakeFiles()


def test_gitlab_commit_returns_sha_with_service_author(monkeypatch):
    monkeypatch.setattr(config, "GITLAB_COMMIT_AUTHOR_NAME", "svc")
    monkeypatch.setattr(config, "GITLAB_COMMIT_AUTHOR_EMAIL", "svc@keep")
    commits = FakeCommits()
    client = GitlabGitClient(project=FakeProject(commits=commits))

    sha = client.commit_script("id/script.py", "print(1)", "create foo")

    assert sha == "deadbeef" * 5
    payload = commits.calls[0]
    assert payload["author_name"] == "svc"
    assert payload["author_email"] == "svc@keep"
    assert payload["actions"][0]["file_path"] == "id/script.py"


def test_gitlab_commit_falls_back_to_create_when_file_absent():
    commits = FakeCommits(missing_on_update=True)
    client = GitlabGitClient(project=FakeProject(commits=commits))

    client.commit_script("id/script.py", "print(1)", "create foo")

    assert [c["actions"][0]["action"] for c in commits.calls] == ["update", "create"]


def test_gitlab_commit_non_missing_400_does_not_fall_back():
    """A 400 that is not 'file absent' (e.g. a protected branch) must surface,
    not be silently retried as a create."""

    class ProtectedBranch(FakeCommits):
        def create(self, payload):
            self.calls.append(payload)
            raise gitlab.exceptions.GitlabCreateError(
                "You are not allowed to push into this branch", response_code=400
            )

    commits = ProtectedBranch()
    client = GitlabGitClient(project=FakeProject(commits=commits))
    with pytest.raises(ScriptRepoUnavailableError):
        client.commit_script("id/script.py", "x", "msg")
    # Only the initial "update" was attempted — no create fallback.
    assert [c["actions"][0]["action"] for c in commits.calls] == ["update"]


def test_gitlab_commit_maps_errors_to_unavailable():
    class AlwaysForbidden(FakeCommits):
        def create(self, payload):
            raise gitlab.exceptions.GitlabCreateError("forbidden", response_code=403)

    client = GitlabGitClient(project=FakeProject(commits=AlwaysForbidden()))
    with pytest.raises(ScriptRepoUnavailableError):
        client.commit_script("id/script.py", "x", "msg")


def test_gitlab_read_returns_decoded_content():
    client = GitlabGitClient(
        project=FakeProject(files=FakeFiles(content=b"def handle(a): pass"))
    )
    assert client.read_script("id/script.py") == "def handle(a): pass"


def test_gitlab_read_missing_returns_none():
    client = GitlabGitClient(project=FakeProject(files=FakeFiles(error_code=404)))
    assert client.read_script("id/script.py") is None


def test_gitlab_read_maps_other_errors_to_unavailable():
    client = GitlabGitClient(project=FakeProject(files=FakeFiles(error_code=500)))
    with pytest.raises(ScriptRepoUnavailableError):
        client.read_script("id/script.py")


def test_gitlab_client_satisfies_protocol():
    assert isinstance(GitlabGitClient(project=FakeProject()), GitClient)


def test_gitlab_call_is_time_bounded_and_returns_promptly(monkeypatch):
    # The fake blocks for 5s; with a 0.2s bound the call must return in well
    # under that (proving the executor is not awaited on shutdown), mapped to
    # the retryable error.
    monkeypatch.setattr(config, "GITLAB_TIMEOUT_SECONDS", 0.2)
    client = GitlabGitClient(project=FakeProject(files=FakeFiles(slow_seconds=5.0)))
    started = time.perf_counter()
    with pytest.raises(ScriptRepoUnavailableError):
        client.read_script("id/script.py")
    assert time.perf_counter() - started < 2.0


def test_build_gitlab_client_fails_fast_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TYPE", "session")  # past the noauth gate
    monkeypatch.setattr(config, "GITLAB_URL", "")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_TOKEN", "")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_PROJECT", "")
    with pytest.raises(RuntimeError) as excinfo:
        build_gitlab_client()
    assert "GITLAB_URL" in str(excinfo.value)


def test_build_gitlab_client_refuses_under_noauth(monkeypatch):
    # Fully configured, but the user tier is still the noauth shim — refuse to
    # serve the real client so unauthenticated callers cannot write to the repo.
    monkeypatch.setattr(config, "AUTH_TYPE", "noauth")
    monkeypatch.setattr(config, "GITLAB_URL", "https://gitlab.example")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_TOKEN", "tok")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_PROJECT", "group/keep-automation-scripts")
    with pytest.raises(RuntimeError) as excinfo:
        build_gitlab_client()
    assert "noauth" in str(excinfo.value).lower()


def test_build_gitlab_client_is_fresh_per_call_when_configured(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TYPE", "session")
    monkeypatch.setattr(config, "GITLAB_URL", "https://gitlab.example")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_TOKEN", "tok")
    monkeypatch.setattr(config, "GITLAB_SCRIPTS_PROJECT", "group/keep-automation-scripts")
    first = build_gitlab_client()
    second = build_gitlab_client()
    # Distinct instances (not a shared singleton) → own requests.Session each.
    assert isinstance(first, GitlabGitClient)
    assert first is not second
