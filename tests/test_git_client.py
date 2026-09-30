"""InMemoryGitClient behavior + protocol conformance."""
from src.bl.git_client import GitClient, InMemoryGitClient


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


def test_archive_marks_path_and_keeps_script_bytes():
    client = InMemoryGitClient()
    client.commit_script("abc/script.py", "v1", "create")
    sha = client.archive("abc", "deleter@keep")
    assert isinstance(sha, str) and len(sha) == 40
    assert client.read_script("abc/.archived") is not None
    assert client.read_script("abc/script.py") == "v1"


def test_archive_is_idempotent_no_second_commit():
    client = InMemoryGitClient()
    first = client.archive("abc", "deleter@keep")
    second = client.archive("abc", "someone-else@keep")
    assert first == second
    assert client.commit_log.count("archive abc") == 1
