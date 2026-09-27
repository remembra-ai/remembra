"""Location-independent project identity.

The same repository checked out on a laptop, an external drive, a server or in
a git worktree must resolve to the same project. A *fingerprint* is derived
from, in order of strength:

1. the git remote URL, normalized so https / ssh / scp-style / ``.git`` /
   credentials / port / case variants collapse to ``host/owner/repo``;
2. the repository's root commit (``git rev-list --max-parents=0 HEAD``);
3. the local path (optionally host-qualified) — machine-specific, last resort.

Pure functions only: shared by the API server and the stdlib CLI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

KIND_GIT = "git"
KIND_ROOT = "root"
KIND_PATH = "path"

# Which new locations a configured project (``hint_project``) may name. A client
# that sends nothing gets ``all``: the 0.16.0 behaviour, where every location the
# server had not seen joined the configured project. ``folders`` (sent by 0.16.1+
# clients unless the user opted into one namespace with REMEMBRA_RELAY_PROJECT):
# a git repository always gets its own project, and the configured project only
# names locations that are not git repositories.
HINT_SCOPE_ALL = "all"
HINT_SCOPE_FOLDERS = "folders"
HINT_SCOPES = (HINT_SCOPE_ALL, HINT_SCOPE_FOLDERS)

_SCP_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[A-Za-z0-9.\-]+):(?!//)(?P<path>.+)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")
_HOST_ALIASES = {"www.github.com": "github.com", "ssh.github.com": "github.com", "altssh.gitlab.com": "gitlab.com"}
MAX_PROJECT_ID_LEN = 64


def _clean_repo_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    path = re.sub(r"/{2,}", "/", path).strip("/")
    if path.startswith("~"):
        path = path[1:].lstrip("/")
    while path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    return path


def normalize_git_remote(url: str | None) -> str | None:
    """Collapse a git remote URL to ``host/owner/repo`` (lower-case).

    ``https://github.com/Owner/Repo.git``, ``git@github.com:owner/repo``,
    ``ssh://git@github.com:22/owner/repo.git`` and
    ``https://user:token@github.com/owner/repo/`` all give
    ``github.com/owner/repo``. Absolute local-path remotes give
    ``local:<path>``. Returns None for empty or unparseable input and for a
    relative local path (the client makes those absolute before sending).
    """
    raw = (url or "").strip()
    if not raw:
        return None
    for prefix in ("git+ssh://", "ssh+git://", "git+https://", "git+http://"):
        if raw.lower().startswith(prefix):
            raw = raw.split("+", 1)[1]
            break

    host: str | None
    path: str
    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme == "file":
            local = _clean_repo_path(parts.path)
            return f"local:/{local.lower()}" if local else None
        host = (parts.hostname or "").lower() or None
        path = parts.path
    else:
        match = _SCP_RE.match(raw)
        if match and not re.match(r"^[A-Za-z]:[\\/]", raw):  # not a Windows drive path
            host, path = match.group("host").lower(), match.group("path")
        else:
            if not raw.startswith(("/", "~", "\\")) and not re.match(r"^[A-Za-z]:[\\/]", raw):
                return None  # relative local path (../upstream): means nothing without its base directory
            local = _clean_repo_path(raw)
            return f"local:/{local.lower()}" if local else None

    if not host:
        return None
    host = _HOST_ALIASES.get(host, host)
    repo_path = _clean_repo_path(path)
    if not repo_path:
        return None
    return f"{host}/{repo_path}".lower()


def normalize_root_commit(sha: str | None) -> str | None:
    """A full (40 or 64 hex) root commit id, lower-cased; None if malformed.

    When a history has several roots the caller should send the
    lexicographically smallest so every clone picks the same one.
    """
    value = (sha or "").strip().lower()
    return value if _SHA_RE.match(value) else None


def normalize_root_path(path: str | None, host: str | None = None) -> str | None:
    """Path fingerprint body: ``host:/abs/path`` (host optional), no trailing slash."""
    value = (path or "").strip().replace("\\", "/")
    if not value:
        return None
    value = re.sub(r"/{2,}", "/", value)
    if len(value) > 1:
        value = value.rstrip("/")
    host_part = (host or "").strip().lower()
    return f"{host_part}:{value}" if host_part else value


def slugify_project(name: str | None) -> str:
    """A safe project id from a repo name: ``[a-z0-9._-]``, max 64 chars."""
    slug = _SLUG_BAD.sub("-", (name or "").strip().lower()).strip("-.")
    return slug[:MAX_PROJECT_ID_LEN].strip("-.") or "project"


@dataclass(frozen=True)
class Fingerprint:
    kind: str
    value: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"


@dataclass(frozen=True)
class ProjectLocator:
    """What a client knows about where it is running."""

    git_remote: str | None = None
    root_commit: str | None = None
    root_path: str | None = None
    repo_name: str | None = None
    host: str | None = None

    def fingerprints(self) -> list[Fingerprint]:
        """Fingerprints in strength order (git remote, root commit, path)."""
        out: list[Fingerprint] = []
        remote = normalize_git_remote(self.git_remote)
        if remote:
            out.append(Fingerprint(KIND_GIT, remote))
        root = normalize_root_commit(self.root_commit)
        if root:
            out.append(Fingerprint(KIND_ROOT, root))
        path = normalize_root_path(self.root_path, self.host)
        if path:
            out.append(Fingerprint(KIND_PATH, path))
        return out

    def display_name(self) -> str | None:
        """Best human repo name: remote's last segment, then repo_name, then path basename."""
        remote = normalize_git_remote(self.git_remote)
        if remote:
            return remote.rstrip("/").rsplit("/", 1)[-1]
        if self.repo_name and self.repo_name.strip():
            return self.repo_name.strip()
        if self.root_path and self.root_path.strip():
            tail = self.root_path.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            return tail or None
        return None

    def is_empty(self) -> bool:
        return not self.fingerprints()

    def is_repository(self, git_repo: bool | None = None) -> bool:
        """True for a git repository: it has a remote or a root commit, or the client said so
        (``git_repo``: a repository with no commit and no remote yet has only its path)."""
        return git_repo is True or any(fp.kind in (KIND_GIT, KIND_ROOT) for fp in self.fingerprints())


def location_record(locator: ProjectLocator, git_repo: bool | None = None) -> dict[str, Any] | None:
    """What a handoff keeps about where its session worked (None without any fingerprint).

    ``fingerprints`` are the keys ``project_fingerprints`` uses, so a handoff can
    be matched to a binding exactly; ``repository`` is the one naming the
    repository (None for a folder); ``name``, ``root_path`` and ``host`` are
    for people reading the brief. ``git_repo`` is True for a repository, False
    for a folder the client said is not one, and None when that is unknown (a
    path alone from a client that did not say, or whose git timed out).
    """
    keys = [fp.key for fp in locator.fingerprints()]
    if not keys:
        return None
    repo: bool | None = True if locator.is_repository(git_repo) else (False if git_repo is False else None)
    return {
        "fingerprints": keys,
        "repository": repository_key(keys),
        "name": locator.display_name(),
        "root_path": (locator.root_path or "").strip() or None,
        "host": (locator.host or "").strip().lower() or None,
        "git_repo": repo,
    }


def repository_key(fingerprint_keys: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """The key that names a repository among fingerprint keys: its git remote, else its root commit.

    None for a folder (only a path). Two checkouts of one repository share it.
    """
    keys = list(fingerprint_keys)
    for kind in (KIND_GIT, KIND_ROOT):
        for key in keys:
            if key.startswith(f"{kind}:"):
                return key
    return None
