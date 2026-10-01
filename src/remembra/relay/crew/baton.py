"""Baton refs: the stalled session's uncommitted work, saved and restored (spec D30, §8.1 "Baton refs").

Creation (stall, lost-by-orphan, dirty SessionEnd, ``release --baton``) builds a commit from a
**temporary index**, so staged, unstaged and untracked files are captured without touching the
working tree or the real index::

    GIT_INDEX_FILE=<tmp> git read-tree HEAD
    GIT_INDEX_FILE=<tmp> git update-index --add --remove -z --stdin   # the dirty paths
    GIT_INDEX_FILE=<tmp> git write-tree
    git commit-tree <tree> -p HEAD                                    # parent = the session's HEAD
    git update-ref refs/remembra/baton/<T-n>/<seq> <commit>

Ignored files are never captured (``git status`` does not list them); files over 5 MB and
``.env*`` files are listed as skipped but never captured. The unpushed commits
(``@{upstream}..HEAD``) are recorded too. A clean checkout with a configured remote
and unpublished commits gets a same-tree baton so a successor clone can fetch its HEAD.

``restore`` (adopt) refuses on a dirty tree and prints the command to run once it is clean; it
never runs a destructive git command on its own. In a clean tree it moves to the baton's parent
(the branch when it is free, else a new ``remembra/<T-n>-<seq>`` branch), checks the saved files
out into the working tree, unstages them and removes files the stalled session had deleted.
Stdlib only.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

BATON_PREFIX: Final = "refs/remembra/baton"
BATON_HEADS_PREFIX: Final = "refs/remembra/baton-heads"
MAX_FILE_BYTES: Final = 5 * 1024 * 1024
REF_TTL_S: Final = 7 * 86400
LABEL_RE: Final = re.compile(r"^(?:T-[1-9][0-9]{0,6}|cs_[A-Za-z0-9_-]{1,64})$")
_ENV_RE: Final = re.compile(r"(?:^|/)\.env(?:\.[^/]*)?$|(?:^|/)[^/]*\.env$")
GIT_IDENTITY: Final = {
    "GIT_AUTHOR_NAME": "Remembra Crew",
    "GIT_AUTHOR_EMAIL": "crew@remembra.invalid",
    "GIT_COMMITTER_NAME": "Remembra Crew",
    "GIT_COMMITTER_EMAIL": "crew@remembra.invalid",
}


class GitError(RuntimeError):
    pass


def git(
    args: Sequence[str],
    cwd: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    # crewd observes a checkout while its agent may be staging/committing.
    # Suppress Git's optional index refresh; explicit writes (including baton
    # temporary indexes and refs) still take their required locks.
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", **(env or {})}
    try:
        res = subprocess.run(  # noqa: S603,S607 - fixed git argv, no shell
            ["git", *args], cwd=str(cwd), capture_output=True, timeout=timeout, env=full_env, input=stdin, check=False
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0] if args else ''} timed out") from e
    except OSError as e:
        raise GitError(f"git not runnable: {e.__class__.__name__}") from e
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {res.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return res


def out(args: Sequence[str], cwd: str | Path, **kw: Any) -> str:
    return git(args, cwd, **kw).stdout.decode("utf-8", "replace").strip()


def try_out(args: Sequence[str], cwd: str | Path, **kw: Any) -> str | None:
    try:
        res = git(args, cwd, check=False, **kw)
    except GitError:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# Repository facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoFacts:
    toplevel: str
    git_dir: str
    git_common_dir: str
    branch: str | None
    head: str | None
    default_branch: str | None
    upstream: str | None
    case_insensitive: bool
    remote: str | None


def _case_insensitive(path: str) -> bool:
    """A case-insensitive volume answers for a path whose case was flipped (checked on the real toplevel)."""
    base = os.path.basename(path.rstrip("/"))
    flipped = base.swapcase()
    if flipped == base:
        probe = os.path.join(path, ".git")
        alt = os.path.join(path, ".GIT")
        return os.path.exists(probe) and os.path.exists(alt)
    try:
        return os.path.samefile(path, os.path.join(os.path.dirname(path.rstrip("/")), flipped))
    except OSError:
        return False


def default_branch(toplevel: str) -> str | None:
    ref = try_out(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], toplevel)
    if ref and ref.startswith("refs/remotes/origin/"):
        return ref[len("refs/remotes/origin/") :]
    for name in ("main", "master", "trunk", "develop"):
        if try_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], toplevel):
            return name
    configured = try_out(["config", "--get", "init.defaultBranch"], toplevel)
    return configured or None


def repo_facts(cwd: str | Path) -> RepoFacts | None:
    top = try_out(["rev-parse", "--show-toplevel"], cwd, timeout=3.0)
    if not top:
        return None
    top = os.path.realpath(top)
    git_dir = try_out(["rev-parse", "--path-format=absolute", "--git-dir"], top) or os.path.join(top, ".git")
    common = try_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], top) or git_dir
    branch = try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
    head = try_out(["rev-parse", "--verify", "--quiet", "HEAD"], top)
    upstream = try_out(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], top)
    remote = None
    if upstream and "/" in upstream:
        remote = upstream.split("/", 1)[0]
    elif try_out(["remote"], top):
        remotes = (try_out(["remote"], top) or "").split()
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else None)
    return RepoFacts(
        toplevel=top,
        git_dir=os.path.realpath(git_dir),
        git_common_dir=os.path.realpath(common),
        branch=branch,
        head=head,
        default_branch=default_branch(top),
        upstream=upstream,
        case_insensitive=_case_insensitive(top),
        remote=remote,
    )


def dirty_entries(toplevel: str | Path) -> list[tuple[str, str]]:
    """``(xy status, path)`` for every changed, deleted or untracked (not ignored) path."""
    res = git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], toplevel, timeout=5.0)
    items = res.stdout.decode("utf-8", "replace").split("\0")
    outl: list[tuple[str, str]] = []
    for item in items:
        if len(item) < 4:
            continue
        outl.append((item[:2], item[3:]))
    return outl


def is_clean(toplevel: str | Path) -> bool:
    return not dirty_entries(toplevel)


def unpushed_commits(toplevel: str | Path, limit: int = 200) -> list[str]:
    upstream = try_out(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], toplevel)
    if upstream:
        text = try_out(["rev-list", f"--max-count={limit}", f"{upstream}..HEAD"], toplevel)
    else:
        text = try_out(["rev-list", f"--max-count={limit}", "HEAD", "--not", "--remotes"], toplevel)
    return (text or "").split()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@dataclass
class BatonRef:
    ref: str
    commit: str
    parent: str
    branch: str | None
    label: str
    seq: int
    dirty_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unpushed: list[str] = field(default_factory=list)
    pushed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "commit": self.commit,
            "parent": self.parent,
            "branch": self.branch,
            "label": self.label,
            "seq": self.seq,
            "dirty_files": self.dirty_files,
            "skipped": self.skipped,
            "unpushed": self.unpushed,
            "pushed": self.pushed,
        }


def check_label(label: str) -> str:
    if not LABEL_RE.match(label or ""):
        raise ValueError(f"baton label must be T-<n> or a session id, got {label!r}")
    return label


def next_seq(toplevel: str | Path, label: str) -> int:
    text = try_out(["for-each-ref", "--format=%(refname)", f"{BATON_PREFIX}/{label}/"], toplevel) or ""
    seqs = [int(r.rsplit("/", 1)[1]) for r in text.split() if r.rsplit("/", 1)[1].isdigit()]
    return max(seqs, default=0) + 1


def _skip(path: str, toplevel: str) -> bool:
    if _ENV_RE.search(path):
        return True
    try:
        st = os.lstat(os.path.join(toplevel, path))
    except OSError:
        return False
    return st.st_size > MAX_FILE_BYTES


def create_baton_ref(toplevel: str | Path, label: str, *, seq: int | None = None) -> BatonRef | None:
    """Save dirty work or unpublished history; None when neither needs a recovery ref."""
    check_label(label)
    top = os.path.realpath(str(toplevel))
    head = try_out(["rev-parse", "--verify", "--quiet", "HEAD"], top)
    if not head:
        return None  # an unborn branch has no parent to hang the baton on
    entries = dirty_entries(top)
    unpublished = unpushed_commits(top)
    # With no configured remote, a clean local repository has no publication
    # boundary to recover. Dirty work still gets the existing local baton.
    preserve_head = bool(unpublished and try_out(["remote"], top))
    if not entries and not preserve_head:
        return None
    paths = sorted({p for _xy, p in entries})
    capture = [p for p in paths if not _skip(p, top)]
    skipped = [p for p in paths if p not in capture]
    git_dir = out(["rev-parse", "--path-format=absolute", "--git-dir"], top)
    index = os.path.join(git_dir, f"remembra-baton-{os.getpid()}-{secrets.token_hex(4)}.index")
    env = {"GIT_INDEX_FILE": index, **GIT_IDENTITY}
    try:
        git(["read-tree", head], top, env=env)
        if capture:
            git(
                ["update-index", "--add", "--remove", "-z", "--stdin"],
                top,
                env=env,
                stdin=b"\0".join(p.encode() for p in capture) + b"\0",
            )
        tree = out(["write-tree"], top, env=env)
    finally:
        try:
            os.unlink(index)
        except OSError:
            pass
    head_tree = out(["rev-parse", f"{head}^{{tree}}"], top)
    if tree == head_tree and not preserve_head:
        return None
    branch = try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
    n = seq if seq is not None else next_seq(top, label)
    message = (
        f"remembra baton {label}/{n}\n\nRemembra-Baton: {label}\nRemembra-Branch: {branch or '(detached)'}\n"
        f"Remembra-Head: {head}\nRemembra-Saved-At: {int(time.time())}\n"
    )
    commit = out(["commit-tree", tree, "-p", head, "-m", message], top, env=GIT_IDENTITY)
    ref = f"{BATON_PREFIX}/{label}/{n}"
    git(["update-ref", ref, commit], top)
    changed = out(["diff", "--name-only", "-z", head, commit], top)
    return BatonRef(
        ref=ref,
        commit=commit,
        parent=head,
        branch=branch,
        label=label,
        seq=n,
        dirty_files=[p for p in changed.split("\0") if p],
        skipped=skipped,
        unpushed=unpublished,
    )


def push_baton(toplevel: str | Path, baton: BatonRef, remote: str, *, timeout: float = 20.0) -> bool:
    """Push the baton ref and the stalled branch head (``baton_push: auto``); never a force push."""
    refspecs = [f"{baton.ref}:{baton.ref}", f"{baton.parent}:{BATON_HEADS_PREFIX}/{baton.label}/{baton.seq}"]
    res = git(["push", "--quiet", remote, *refspecs], toplevel, timeout=timeout, check=False)
    baton.pushed = res.returncode == 0
    return baton.pushed


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@dataclass
class RestoreResult:
    restored: bool
    reason: str
    ref: str
    files: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    branch: str | None = None
    next_command: str | None = None
    error: str | None = None  # restore_failed: what failed
    rolled_back: bool | None = None  # restore_failed: the checkout is back to its state before the restore

    def as_dict(self) -> dict[str, Any]:
        return {
            "restored": self.restored,
            "reason": self.reason,
            "ref": self.ref,
            "files": self.files,
            "deleted": self.deleted,
            "branch": self.branch,
            "next_command": self.next_command,
            "error": self.error,
            "rolled_back": self.rolled_back,
        }


def _trailer(message: str, name: str) -> str | None:
    m = re.search(rf"^{re.escape(name)}: (.+)$", message, re.MULTILINE)
    return m.group(1).strip() if m else None


UNATTRIBUTED_SCAN_MAX: Final = 50
MEMBER_TRAILER: Final = "Remembra-Member"


def unattributed_commits(toplevel: str | Path, since: str, head: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Commits in ``since..head`` with no ``Remembra-Member`` trailer, each with the files it changed (§8.4).

    A commit made outside every crew session (a human at a terminal, or an agent whose hooks
    could not identify it) carries no trailer. The server turns the ones touching a held zone
    into ``unattributed_change`` notices. Merge commits are skipped (their first-parent diff is
    other people's landed work). Returns ``[{"sha", "files"}]``, oldest first.
    """
    revs = try_out(
        ["rev-list", "--no-merges", f"--max-count={UNATTRIBUTED_SCAN_MAX}", "--reverse", head, "--not", since], toplevel
    )
    if not revs:
        return []
    found: list[dict[str, Any]] = []
    for sha in revs.split():
        message = try_out(["log", "-1", "--format=%B", sha], toplevel)
        if message is None or _trailer(message, MEMBER_TRAILER):
            continue
        names = try_out(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", sha], toplevel)
        files = [f for f in (names or "").splitlines() if f][:100]
        if files:
            found.append({"sha": sha, "files": files})
        if len(found) >= limit:
            break
    return found


def _checked_out_branches(toplevel: str) -> set[str]:
    text = try_out(["worktree", "list", "--porcelain"], toplevel) or ""
    return {line.split(" ", 1)[1].removeprefix("refs/heads/") for line in text.splitlines() if line.startswith("branch ")}


def fetch_baton(toplevel: str | Path, ref: str, remote: str, *, timeout: float = 20.0) -> bool:
    res = git(["fetch", "--quiet", "--no-tags", remote, f"+{ref}:{ref}"], toplevel, timeout=timeout, check=False)
    return res.returncode == 0


PATHSPEC_CHUNK = 500  # paths per git call (a large baton never hits ARG_MAX)


def _chunks(items: Sequence[str], n: int = PATHSPEC_CHUNK) -> list[list[str]]:
    return [list(items[i : i + n]) for i in range(0, len(items), n)]


def restore_baton(
    toplevel: str | Path, ref: str, *, remote: str | None = None, retry_command: str | None = None
) -> RestoreResult:
    """Bring a baton's saved work into ``toplevel`` (§8.1). Refuses on a dirty tree; never destructive.

    All or nothing: the tree is clean before anything is touched, so on any failure part-way (a
    read-only fence, a permission error, a git error) the checkout is put back exactly as it was:
    original branch or HEAD, index and tracked files reset to that HEAD, files the restore created
    removed, a branch it created deleted. The result then says ``restore_failed`` with the error.
    """
    top = os.path.realpath(str(toplevel))
    if not try_out(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], top):
        if not (remote and fetch_baton(top, ref, remote)):
            return RestoreResult(False, "ref_missing", ref)
    if not is_clean(top):
        return RestoreResult(False, "dirty_tree", ref, next_command=retry_command)
    commit = out(["rev-parse", f"{ref}^{{commit}}"], top)
    parent = out(["rev-parse", f"{commit}^"], top)
    message = out(["log", "-1", "--format=%B", commit], top)
    saved_branch = _trailer(message, "Remembra-Branch")
    orig_head = try_out(["rev-parse", "--verify", "--quiet", "HEAD"], top)
    orig_branch = try_out(["symbolic-ref", "--quiet", "--short", "HEAD"], top)
    branch = orig_branch
    changed = [p for p in out(["diff", "--name-only", "-z", "--diff-filter=d", parent, commit], top).split("\0") if p]
    deleted = [p for p in out(["diff", "--name-only", "-z", "--diff-filter=D", parent, commit], top).split("\0") if p]
    existed = {p for p in changed if os.path.lexists(os.path.join(top, p))}
    created_branch: str | None = None
    try:
        if orig_head != parent:
            taken = _checked_out_branches(top)
            if (
                saved_branch
                and saved_branch != "(detached)"
                and saved_branch not in taken
                and (try_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{saved_branch}"], top) == parent)
            ):
                git(["checkout", "--quiet", saved_branch], top)
                branch = saved_branch
            else:
                label_seq = ref[len(BATON_PREFIX) + 1 :].replace("/", "-")
                new = f"remembra/{label_seq}"
                k = 1
                while try_out(["rev-parse", "--verify", "--quiet", f"refs/heads/{new}"], top):
                    k += 1
                    new = f"remembra/{label_seq}-{k}"
                git(["checkout", "--quiet", "-b", new, parent], top)
                created_branch = branch = new
        for chunk in _chunks(changed):
            git(["checkout", commit, "--", *chunk], top, timeout=60.0)
        for chunk in _chunks(changed):
            git(["reset", "--quiet", "--", *chunk], top, timeout=60.0)
        for p in deleted:
            try:
                os.unlink(os.path.join(top, p))
            except FileNotFoundError:
                pass
    except (GitError, OSError) as e:
        detail = str(e)[:300] or e.__class__.__name__
        rolled_back = _roll_back(top, orig_head, orig_branch, created_branch, changed, existed)
        return RestoreResult(
            False,
            "restore_failed",
            ref,
            branch=orig_branch,
            next_command=retry_command,
            error=detail,
            rolled_back=rolled_back,
        )
    return RestoreResult(True, "restored", ref, files=changed, deleted=deleted, branch=branch)


def _roll_back(
    top: str,
    orig_head: str | None,
    orig_branch: str | None,
    created_branch: str | None,
    changed: Sequence[str],
    existed: set[str],
) -> bool:
    """Put a checkout back as it was before a failed restore (it was clean then). True when it is clean again."""
    if orig_head:
        target = orig_branch or orig_head
        git(["checkout", "--quiet", "--force", target], top, check=False, timeout=60.0)
        git(["reset", "--quiet", "--hard", orig_head], top, check=False, timeout=60.0)
    for p in changed:
        if p in existed:
            continue
        full = os.path.join(top, p)
        if os.path.lexists(full) and not try_out(["ls-files", "--error-unmatch", "--", p], top):
            try:
                os.unlink(full)
            except OSError:
                pass
    if created_branch and created_branch != orig_branch:
        git(["branch", "--quiet", "-D", created_branch], top, check=False)
    try:
        return is_clean(top) and try_out(["rev-parse", "--verify", "--quiet", "HEAD"], top) == orig_head
    except GitError:
        return False


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def list_batons(toplevel: str | Path) -> list[str]:
    text = try_out(["for-each-ref", "--format=%(refname)", f"{BATON_PREFIX}/"], toplevel) or ""
    return text.split()


def delete_baton(toplevel: str | Path, ref: str, *, remote: str | None = None) -> None:
    git(["update-ref", "-d", ref], toplevel, check=False)
    if remote:
        git(["push", "--quiet", remote, f":{ref}"], toplevel, timeout=20.0, check=False)


def expired(registry: Mapping[str, Mapping[str, Any]], now: float, ttl_s: float = REF_TTL_S) -> list[str]:
    """Refs whose adopt or release is older than ``ttl_s`` (§4.5: deleted 7 days after adopt or release)."""
    return [ref for ref, rec in registry.items() if rec.get("closed_at") and now - float(rec["closed_at"]) >= ttl_s]


def label_for(task_number: int | None, session_id: str) -> str:
    return f"T-{task_number}" if task_number else session_id


def changed_paths(entries: Iterable[tuple[str, str]]) -> list[str]:
    return sorted({p for _xy, p in entries})
