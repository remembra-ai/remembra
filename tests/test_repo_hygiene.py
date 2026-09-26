"""Nothing private reaches the public repository (scripts/ci/repo_hygiene.py, hooks/pre-push).

The repository is public, so a private note is published the moment a commit
that carries it is pushed, even if a later commit deletes it. These tests
plant private material in throw-away repositories and check that the tree
check and the pre-push guard refuse it, and that the real tree is clean.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "repo_hygiene.py"
HOOK = ROOT / "hooks" / "pre-push"

pytestmark = pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs a git checkout of the repository")


def _hygiene():
    spec = importlib.util.spec_from_file_location("repo_hygiene", SCRIPT)
    assert spec is not None and spec.loader is not None, f"{SCRIPT} is missing"
    module = importlib.util.module_from_spec(spec)
    sys.modules["repo_hygiene"] = module
    spec.loader.exec_module(module)
    return module


def _env(home: Path) -> dict[str, str]:
    # A clean git: no global or system config (hooksPath, signing, templates) from the developer's machine.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
        PYTHON=sys.executable,
    )
    return env


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, env=_env(cwd.parent), capture_output=True, text=True, timeout=60, check=check)


def _commit(repo: Path, files: dict[str, str | bytes], message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        _git(repo, "add", "--force", rel)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def pushable(tmp_path: Path) -> tuple[Path, Path]:
    """A work repository with the pre-push guard installed, and an empty bare remote."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=_env(tmp_path))
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=_env(tmp_path))
    _git(work, "remote", "add", "origin", str(remote))
    hooks = work / ".git" / "hooks"
    shutil.copy(HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    # The hook runs the checker from the working tree; install-hooks.sh also leaves a copy next to it.
    shutil.copy(SCRIPT, hooks / "repo_hygiene.py")
    return work, remote


def _push(work: Path, *refspec: str) -> subprocess.CompletedProcess[str]:
    return _git(work, "push", "origin", *(refspec or ("HEAD",)), check=False)


def _remote_head(remote: Path, branch: str) -> str:
    done = _git(remote, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    return done.stdout.strip()


# ---------------------------------------------------------------------------
# LEAK-6: the remediation tracker (docs/audits/, *-fixlog.md) is never pushed
# ---------------------------------------------------------------------------


def test_pre_push_guard_refuses_a_branch_that_carries_docs_audits(pushable) -> None:
    work, remote = pushable
    first = _commit(work, {"README.md": "public\n"}, "init")
    done = _push(work, "main")
    assert done.returncode == 0, done.stderr
    assert _remote_head(remote, "main") == first

    _commit(work, {"docs/audits/2026-09-25-tracker.md": "open findings\n"}, "tracker")
    done = _push(work, "main")
    assert done.returncode != 0
    assert "docs/audits/2026-09-25-tracker.md" in done.stderr
    assert _remote_head(remote, "main") == first

    # Deleting it in a later commit does not help: the push would still publish the commit that added it.
    _git(work, "rm", "-q", "docs/audits/2026-09-25-tracker.md")
    _git(work, "commit", "-q", "-m", "drop tracker")
    done = _push(work, "main")
    assert done.returncode != 0 and "docs/audits/2026-09-25-tracker.md" in done.stderr
    assert _remote_head(remote, "main") == first


def test_pre_push_guard_refuses_a_fixlog_on_a_new_branch_and_lets_clean_work_through(pushable) -> None:
    work, remote = pushable
    _commit(work, {"README.md": "public\n"}, "init")
    assert _push(work, "main").returncode == 0

    _git(work, "checkout", "-q", "-b", "fix/sec")
    _commit(work, {"notes/sec-fixlog.md": "what was fixed where\n"}, "fixlog")
    done = _push(work, "fix/sec")
    assert done.returncode != 0 and "notes/sec-fixlog.md" in done.stderr
    assert _remote_head(remote, "fix/sec") == ""

    _git(work, "checkout", "-q", "main")
    _git(work, "checkout", "-q", "-b", "fix/clean")
    clean = _commit(work, {"src/app.py": "print('hello')\n"}, "clean change")
    done = _push(work, "fix/clean")
    assert done.returncode == 0, done.stderr
    assert _remote_head(remote, "fix/clean") == clean


def test_pre_push_guard_falls_back_to_the_installed_checker(pushable) -> None:
    # A branch from before the checker existed is still guarded, by the copy install-hooks.sh left in .git/hooks.
    work, _remote = pushable
    _commit(work, {"README.md": "public\n", "docs/audits/x.md": "private\n"}, "old branch")
    assert not (work / "scripts" / "ci" / "repo_hygiene.py").exists()
    done = _push(work, "main")
    assert done.returncode != 0 and "docs/audits/x.md" in done.stderr


def test_range_check_sees_a_merge_that_adds_private_notes(tmp_path: Path) -> None:
    hygiene = _hygiene()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    base = _commit(repo, {"README.md": "ok\n"}, "init")
    _git(repo, "checkout", "-q", "-b", "side")
    _commit(repo, {"side.py": "x = 1\n"}, "side")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {"main.py": "y = 2\n"}, "main")
    # A merge whose resolution adds a file neither parent had.
    _git(repo, "merge", "-q", "--no-ff", "--no-commit", "side")
    (repo / "docs" / "audits").mkdir(parents=True)
    (repo / "docs" / "audits" / "merged.md").write_text("private\n")
    _git(repo, "add", "--force", "docs/audits/merged.md")
    _git(repo, "commit", "-q", "-m", "merge side")
    found = hygiene.check_commits(repo, hygiene.commits_in_range(repo, f"{base}..HEAD"))
    assert [(f.path, f.rule) for f in found] == [("docs/audits/merged.md", "private-notes")]
    assert found[0].commit == _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_checker_runs_on_the_system_python3() -> None:
    # hooks/pre-push runs the checker with whatever python3 is on PATH (3.9 on macOS).
    python3 = shutil.which("python3")
    if python3 is None:
        pytest.skip("no python3 on PATH")
    done = subprocess.run([python3, str(SCRIPT), "tree"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode in (0, 1), done.stderr
    assert "Traceback" not in done.stderr, done.stderr


def test_tree_check_flags_private_notes(tmp_path: Path) -> None:
    hygiene = _hygiene()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    _commit(repo, {"README.md": "ok\n", "docs/audits/a.md": "x\n", "docs/rel-fixlog.md": "y\n"}, "plant")
    found = {f.path: f.rule for f in hygiene.check_tree(repo)}
    assert found == {"docs/audits/a.md": "private-notes", "docs/rel-fixlog.md": "private-notes"}


# ---------------------------------------------------------------------------
# LEAK-3 / CLI-06 / DEP-LEAK-1: no built site, no copy of the feedback transcript
# ---------------------------------------------------------------------------

# Built at run time, one half per line, so this file does not itself match the rule.
TRANSCRIPT_TITLE = " ".join(
    [
        "Remembra Feedback And",
        "Codex Setup Redesign",
    ]
)


def test_tree_check_flags_a_built_site_and_vercel_link(tmp_path: Path) -> None:
    hygiene = _hygiene()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    _commit(
        repo,
        {"site/index.html": "<html></html>\n", "landing/.vercel/project.json": "{}\n", "docs/feedback/x.md": "x\n"},
        "plant",
    )
    found = {(f.path, f.rule) for f in hygiene.check_tree(repo)}
    assert found == {
        ("site/index.html", "built-site"),
        ("landing/.vercel/project.json", "built-site"),
        ("docs/feedback/x.md", "private-notes"),
    }


def test_a_copy_of_the_feedback_transcript_is_refused_anywhere(tmp_path: Path, pushable) -> None:
    hygiene = _hygiene()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    _commit(repo, {"notes/old.html": f"<title>{TRANSCRIPT_TITLE} - Remembra</title>\n", "ok.md": "fine\n"}, "plant")
    assert [(f.path, f.line, f.rule) for f in hygiene.check_tree(repo)] == [("notes/old.html", 1, "feedback-transcript")]

    work, remote = pushable
    _commit(work, {"README.md": "public\n"}, "init")
    assert _push(work, "main").returncode == 0
    _commit(work, {"docs/guides/notes.md": f"# {TRANSCRIPT_TITLE}\n"}, "copy")
    done = _push(work, "main")
    assert done.returncode != 0 and "feedback-transcript" in done.stderr


@pytest.mark.parametrize("path", ["docs/audits/2026-09-25-sec.md", "docs/sec-fixlog.md", "rel-fixlog.md"])
def test_gitignore_keeps_private_notes_out(path: str) -> None:
    done = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, capture_output=True, check=False)
    assert done.returncode == 0, f"{path} is not ignored"


def test_install_hooks_installs_the_pre_push_guard_from_a_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    for rel in ("scripts/install-hooks.sh", "hooks/pre-commit", "hooks/pre-push", "scripts/ci/repo_hygiene.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, repo / rel)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "fix/x", str(worktree))
    assert (worktree / ".git").is_file()

    done = subprocess.run(
        ["bash", "scripts/install-hooks.sh"], cwd=worktree, env=_env(tmp_path), capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stdout + done.stderr
    hooks = repo / ".git" / "hooks"  # the shared hooks folder, which every worktree uses
    for name in ("pre-commit", "pre-push", "repo_hygiene.py"):
        assert (hooks / name).is_file(), name
    assert os.access(hooks / "pre-push", os.X_OK)
    assert (hooks / "pre-push").read_bytes() == HOOK.read_bytes()


def test_the_repository_tree_is_clean() -> None:
    hygiene = _hygiene()
    findings = hygiene.check_tree(ROOT)
    assert not findings, "\n".join(str(f) for f in findings)


# ---------------------------------------------------------------------------
# LEAK-1 / CLI-05: no server address in the tree
# ---------------------------------------------------------------------------


def _ip(*octets: int) -> str:
    # Built at run time so this file holds no address literal of its own.
    return ".".join(str(o) for o in octets)


@pytest.mark.parametrize(
    "line",
    [
        f"server = '{_ip(178, 62, 10, 5)}'",
        f"curl --resolve api.example.com:443:{_ip(95, 217, 3, 4)} https://api.example.com/health",
        f"http://{_ip(49, 12, 200, 7)}:8000/health",
        f"The origin is {_ip(49, 12, 200, 7)}.",
    ],
)
def test_a_public_ipv4_address_is_refused(line: str) -> None:
    hygiene = _hygiene()
    found = hygiene.content_findings("tests/test_example.py", line)
    assert [(f.line, f.rule) for f in found] == [(1, "public-ip")]


@pytest.mark.parametrize(
    "line",
    [
        f"host {_ip(203, 0, 113, 10)} and {_ip(198, 51, 100, 7)} and {_ip(192, 0, 2, 1)}",  # documentation ranges
        f"{_ip(127, 0, 0, 1)} {_ip(10, 0, 0, 1)} {_ip(192, 168, 1, 100)} {_ip(172, 16, 0, 5)} {_ip(0, 0, 0, 0)}",
        f'"{_ip(173, 245, 48, 0)}/20",  # a published provider range, not a host',
        f"http://{_ip(224, 0, 0, 1)}/x",  # multicast
        "release 12.8.4.1.2 has five parts, so it is not an address",
        f"spoof = {_ip(1, 2, 3, 4)}",  # the allow-listed placeholders tests use
    ],
)
def test_documentation_private_and_placeholder_addresses_pass(line: str) -> None:
    hygiene = _hygiene()
    assert [str(f) for f in hygiene.content_findings("tests/test_example.py", line) if f.rule == "public-ip"] == []


def test_lock_files_are_not_scanned_for_addresses() -> None:
    hygiene = _hygiene()
    line = f'version = "{_ip(12, 8, 4, 1)}"'
    assert hygiene.content_findings("uv.lock", line) == []
    assert hygiene.content_findings("dashboard/package-lock.json", line) == []
    assert [f.rule for f in hygiene.content_findings("src/app.py", line)] == ["public-ip"]


def test_the_pre_push_guard_refuses_a_server_address(pushable) -> None:
    work, remote = pushable
    first = _commit(work, {"README.md": "public\n"}, "init")
    assert _push(work, "main").returncode == 0
    _commit(work, {"tests/test_deploy.py": f"HOST = '{_ip(178, 62, 10, 5)}'\n"}, "fixture")
    done = _push(work, "main")
    assert done.returncode != 0 and "tests/test_deploy.py:1" in done.stderr and "public-ip" in done.stderr
    assert _remote_head(remote, "main") == first


# ---------------------------------------------------------------------------
# LEAK-5: internal runbooks, confidential plans and old reports stay private
# ---------------------------------------------------------------------------

MOVED_TO_PRIVATE = [
    "docs/DEPLOYING.md",  # the hosted service's runbook; docs/OPERATIONS.md is the public guide
    "docs/bugs/BUG-001-PROJECTSWITCHER-STATE.md",
    "docs/bugs/BUG-002-NAMESPACE-MISMATCH.md",
    "docs/competitive-analysis-2026.md",
    "REMEMBRA-BUILD-PLAN.docx",
    "REMEMBRA-DEEP-RESEARCH-REPORT.docx",
    "REMEMBRA-PRODUCTION-GO-LIVE-GUIDE.docx",
    "REMEMBRA-REMEDIATION-PLAN.docx",
    "SECURITY-AUDIT-2026-06.md",
    "SECURITY-LOCKDOWN-REPORT.md",
]

# Built at run time, one piece per line, so this file does not match the rules itself.
MARKER = "".join(
    [
        "CONFIDEN",
        "TIAL",
    ]
)
SSH_ALIAS = " ".join(
    [
        "ssh",
        "coolify",
    ]
)


def test_internal_runbooks_and_confidential_docs_are_not_tracked() -> None:
    tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split())
    assert not tracked & set(MOVED_TO_PRIVATE), sorted(tracked & set(MOVED_TO_PRIVATE))
    assert not [p for p in tracked if p.startswith("docs/bugs/")]


def test_tree_check_flags_office_documents_and_confidential_markers(tmp_path: Path) -> None:
    hygiene = _hygiene()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_env(tmp_path))
    _commit(
        repo,
        {
            "PLAN.docx": b"PK\x03\x04\x00\x00binary",
            "notes/plan.md": f"# Plan\n\n**{MARKER}**: not for distribution\n",
            "notes/ok.md": "keep this confidential, please\n",  # ordinary prose is fine
        },
        "plant",
    )
    found = sorted((f.path, f.line, f.rule) for f in hygiene.check_tree(repo))
    assert found == [("PLAN.docx", 0, "office-document"), ("notes/plan.md", 3, "confidential")]


def test_tree_check_flags_the_production_host_alias(tmp_path: Path) -> None:
    hygiene = _hygiene()
    found = hygiene.content_findings("docs/OPERATIONS.md", f"   {SSH_ALIAS} 'docker ps'\n")
    assert [(f.line, f.rule) for f in found] == [(1, "private-infra")]


def test_the_pre_push_guard_refuses_an_office_document(pushable) -> None:
    work, remote = pushable
    first = _commit(work, {"README.md": "public\n"}, "init")
    assert _push(work, "main").returncode == 0
    _commit(work, {"REMEMBRA-NEW-PLAN.docx": b"PK\x03\x04\x00\x00binary"}, "plan")
    done = _push(work, "main")
    assert done.returncode != 0 and "REMEMBRA-NEW-PLAN.docx" in done.stderr
    assert _remote_head(remote, "main") == first


# ---------------------------------------------------------------------------
# LEAK-8: test fixtures carry no personal facts about the owner
# ---------------------------------------------------------------------------


def test_fixtures_with_the_owners_personal_facts_are_refused() -> None:
    hygiene = _hygiene()
    # Neutral stand-ins pass; the rule's own words are hashed, so they are not spelled out here.
    for path in ("tests/test_example.py", "scripts/maintenance/seed.py"):
        ok = 'CONTENT = "Ava moved to Lisbon in 2024. She runs a small bakery."'
        assert [f for f in hygiene.content_findings(path, ok) if f.rule == "owner-personal"] == []
    remote = "git@github.com:" + "fresh" + "vybz/app.git"  # split so this file does not match
    found = hygiene.content_findings("tests/test_example.py", f'REMOTE = "{remote}"')
    assert [f.rule for f in found] == ["owner-personal"]
    assert hygiene.content_findings("docs/guides/example.md", f'REMOTE = "{remote}"') == []


def test_the_owners_second_city_is_refused_too() -> None:
    """LEAK-8 review: a billing fixture moved "the office" to the owner's other city (rot13 here, so
    this file does not spell it)."""
    import codecs

    hygiene = _hygiene()
    city = codecs.decode("Zbagrtb Onl", "rot13")
    found = hygiene.content_findings("tests/test_billing.py", f'FACT = "Someone moved the office to {city}"')
    assert [f.rule for f in found] == ["owner-personal"]
    # Each word alone is not the phrase.
    for word in city.split():
        assert hygiene.content_findings("tests/test_billing.py", f'FACT = "{word}"') == []


def test_the_pre_push_guard_judges_only_the_lines_a_push_adds(pushable) -> None:
    # A line that is already on the remote was published before; editing its file elsewhere must not
    # block a push (the tree check in CI keeps reporting it until someone removes it).
    work, remote = pushable
    first = _commit(work, {"tests/test_old.py": f"HOST = '{_ip(95, 217, 3, 4)}'\n"}, "old fixture")
    assert _push(work, "main").returncode != 0  # a new remote: the line is new, so it is refused
    _git(work, "push", "--no-verify", "origin", "main")  # stand-in for history that is already public
    assert _remote_head(remote, "main") == first

    clean = _commit(work, {"tests/test_old.py": f"HOST = '{_ip(95, 217, 3, 4)}'\nPORT = 8443\n"}, "unrelated edit")
    done = _push(work, "main")
    assert done.returncode == 0, done.stderr
    assert _remote_head(remote, "main") == clean

    _commit(work, {"tests/test_old.py": f"HOST = '{_ip(95, 217, 3, 4)}'\nPORT = 8443\nBACKUP = '{_ip(95, 217, 3, 5)}'\n"}, "new")
    done = _push(work, "main")
    assert done.returncode != 0 and "tests/test_old.py:3" in done.stderr and "tests/test_old.py:1" not in done.stderr
    assert _remote_head(remote, "main") == clean


# ---------------------------------------------------------------------------
# LEAK-2: a real-format Remembra API key never reaches a commit or a push
# ---------------------------------------------------------------------------


def _fake_remembra_key() -> str:
    import secrets

    return "rem" + "_" + secrets.token_urlsafe(32).replace("-", "a") + "Z9"  # assembled at runtime, never real


def test_a_remembra_api_key_in_any_tracked_file_is_refused() -> None:
    hygiene = _hygiene()
    key = _fake_remembra_key()
    for path in ("demo/demo_fast.py", "docs/guides/quickstart.md", "tests/test_x.py"):
        found = hygiene.content_findings(path, f'client = Remembra(api_key="{key}")\n')
        assert [(f.line, f.rule) for f in found] == [(1, "api-key")], path
        assert key not in str(found[0])  # the finding never repeats the key
    # Placeholders, masked keys and long identifiers are not keys.
    for text in (
        'api_key="rem_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"',
        "rem_embedding_dimension_setting_is_only_a_long_variable_name_here",
        'REMEMBRA_API_KEY="rem_..."',
        'key = "rem_" + secrets.token_urlsafe(32)',
    ):
        assert [f for f in hygiene.content_findings("tests/test_x.py", text) if f.rule == "api-key"] == [], text


def test_the_pre_push_guard_refuses_a_committed_remembra_key(pushable) -> None:
    work, remote = pushable
    first = _commit(work, {"README.md": "public\n"}, "init")
    assert _push(work, "main").returncode == 0
    key = _fake_remembra_key()
    _commit(work, {"demo/demo_fast.py": f'API_KEY = "{key}"\n'}, "demo")
    _commit(work, {"demo/demo_fast.py": 'API_KEY = os.environ["REMEMBRA_API_KEY"]\n'}, "remove the key again")
    done = _push(work, "main")
    assert done.returncode != 0 and "demo/demo_fast.py:1: [api-key]" in done.stderr
    assert key not in done.stderr
    assert _remote_head(remote, "main") == first


def test_the_pre_commit_hook_refuses_a_staged_remembra_key(tmp_path: Path) -> None:
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=_env(tmp_path))
    hook = work / ".git" / "hooks" / "pre-commit"
    shutil.copy(ROOT / "hooks" / "pre-commit", hook)
    hook.chmod(0o755)
    _commit(work, {"README.md": "public\n"}, "init")  # a clean commit passes the hook
    key = _fake_remembra_key()
    (work / "demo.py").write_text(f'API_KEY = "{key}"\n')
    _git(work, "add", "demo.py")
    done = _git(work, "commit", "-q", "-m", "demo", check=False)
    assert done.returncode != 0
    output = done.stdout + done.stderr
    assert "demo.py" in output and "Remembra API key" in output
    assert key not in output
