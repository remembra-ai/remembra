"""The crewd zones.yml compiler and upload rule (spec D9, D26, D28, §8.1 "zones.yml handling").

The gate never parses YAML (D26). crewd does, with the same parser and validator the server
uses (:func:`remembra.crew.policy.parse_zones_yaml`: closed objects, no YAML aliases, 32 KB cap,
argv-prefix command grammar), and writes the compiled JSON next to the snapshot.

**What gets uploaded** (``PUT /crews/{id}/zones/file``): only the version **committed on the
default branch** (read with ``git show <default>:.remembra/zones.yml``, never the working copy),
or a working copy a human saves with ``remembra-crew zones push`` at an interactive TTY. An
agent's uncommitted edit (which the gate denies anyway, crew-policy row 2) or a commit on a
feature branch is never uploaded; a human merges it first. The server then applies
non-loosening changes and holds loosening ones as pending until a human approves (D9).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from remembra.crew import policy as P
from remembra.crew import schemas as S
from remembra.relay.crew.baton import GitError, git, try_out
from remembra.relay.crew.outbox import atomic_write_json

ZONES_REL: Final = ".remembra/zones.yml"


@dataclass(frozen=True)
class ZonesFile:
    text: str
    sha: str
    source: str  # default_branch | working_copy
    branch: str


@dataclass
class CompileResult:
    ok: bool
    sha: str
    compiled: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def committed_on_default(toplevel: str, default_branch: str | None) -> ZonesFile | None:
    """zones.yml as committed on the default branch (local branch first, then its remote-tracking ref)."""
    if not default_branch:
        return None
    for rev in (f"refs/heads/{default_branch}", f"refs/remotes/origin/{default_branch}"):
        if try_out(["rev-parse", "--verify", "--quiet", rev], toplevel) is None:
            continue
        try:
            res = git(["show", f"{rev}:{ZONES_REL}"], toplevel, check=False)
        except GitError:
            return None
        if res.returncode != 0:
            return None
        text = res.stdout.decode("utf-8", "replace")
        return ZonesFile(text, sha_of(text.rstrip("\n")), "default_branch", default_branch)
    return None


def working_copy(toplevel: str, branch: str | None) -> ZonesFile | None:
    path = Path(toplevel) / ZONES_REL
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return ZonesFile(text, sha_of(text.rstrip("\n")), "working_copy", branch or "(working copy)")


def compile_text(text: str) -> CompileResult:
    """Parse and validate with the server's compiler; never raises."""
    sha = sha_of(text.rstrip("\n"))
    if len(text.encode("utf-8")) > S.MAX_ZONES_YML_BYTES:
        return CompileResult(False, sha, errors=[f"zones.yml is larger than {S.MAX_ZONES_YML_BYTES} bytes"])
    try:
        policy = P.parse_zones_yaml(text)
    except P.PolicyError as e:
        return CompileResult(False, sha, errors=list(e.errors)[:20])
    except Exception as e:  # a YAML library error the policy module did not wrap
        return CompileResult(False, sha, errors=[f"{e.__class__.__name__}: {str(e)[:200]}"])
    return CompileResult(True, sha, compiled=policy.to_dict())


def compiled_path(snapshot_dir: Path, crew_id: str) -> Path:
    safe = "".join(ch for ch in crew_id if ch.isalnum() or ch in "_-")[:80]
    return snapshot_dir / f"{safe}.zones.json"


def write_compiled(snapshot_dir: Path, crew_id: str, source: ZonesFile, result: CompileResult) -> Path:
    path = compiled_path(snapshot_dir, crew_id)
    atomic_write_json(
        path,
        {
            "sha": result.sha,
            "source": source.source,
            "branch": source.branch,
            "ok": result.ok,
            "errors": result.errors,
            "policy": result.compiled,
        },
    )
    return path


@dataclass
class UploadPlan:
    upload: bool
    reason: str
    file: ZonesFile | None = None
    result: CompileResult | None = None


def plan_upload(
    toplevel: str,
    default_branch: str | None,
    last_uploaded_sha: str | None,
    *,
    human_push: bool = False,
    branch: str | None = None,
) -> UploadPlan:
    """Decide whether (and what) to upload for one checkout (§8.1 upload rule)."""
    file = working_copy(toplevel, branch) if human_push else committed_on_default(toplevel, default_branch)
    if file is None:
        return UploadPlan(False, "no_zones_file")
    result = compile_text(file.text)
    if not result.ok:
        return UploadPlan(False, "invalid", file, result)
    if not human_push and result.sha == last_uploaded_sha:
        return UploadPlan(False, "unchanged", file, result)
    return UploadPlan(True, "human_push" if human_push else "default_branch", file, result)


def upload_body(plan: UploadPlan) -> dict[str, Any]:
    assert plan.file is not None and plan.result is not None
    return {"yaml": plan.file.text, "sha": plan.result.sha, "branch": plan.file.branch[:256]}
