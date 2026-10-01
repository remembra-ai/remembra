"""Background Git observations must leave a user's index untouched."""

import os

from remembra.relay.crew import baton as B


def test_background_status_preserves_index_when_only_file_stat_changed(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)
    B.git(["init", "-b", "main"], tmp_path)
    B.git(["config", "user.name", "Synthetic Crew Test"], tmp_path)
    B.git(["config", "user.email", "crew-test@example.invalid"], tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("unchanged content\n")
    B.git(["add", "tracked.txt"], tmp_path)
    B.git(["commit", "-m", "fixture"], tmp_path)
    index = tmp_path / ".git" / "index"
    before = index.read_bytes()
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    result = B.git(["status", "--porcelain=v1"], tmp_path)
    assert result.stdout == b""
    assert index.read_bytes() == before, "A background observation refreshed the user's index"
    # Mandatory writes still work: preventing optional refresh must not prevent
    # creating a baton or a user's ordinary Git operation.
    tracked.write_text("new content\n")
    B.git(["add", "tracked.txt"], tmp_path)
    B.git(["commit", "-m", "second fixture"], tmp_path)
    assert B.out(["show", "HEAD:tracked.txt"], tmp_path) == "new content"
