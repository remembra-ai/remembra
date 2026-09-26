"""The read-only fence for advisory agents (spec §8.3, ``readonly_fence_for_advisory``, Q15).

When an **advisory** session (Codex, Cursor, unverified adapters) works in its **own
worktree** and another session holds a zone exclusive (active or offered) or reserved, crewd
removes the write bits (``chmod a-w``) from that zone's existing files and directories **in the
advisory session's worktree**. That blocks writes from any tool, including Codex
``apply_patch``. Original modes are recorded in ``fence/<checkout>.json`` and restored when the
zone is released, when the session ends, and at every crewd start (``sweep``).

It is cooperative only: an agent can ``chmod`` back. The post-tool git delta and the pre-push gate
catch writes made that way. Stdlib only (+ the gatecore glob matcher).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from remembra.crew import gatecore as G
from remembra.relay.crew.outbox import atomic_write_json

WRITE_BITS: Final = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
BLOCKING_STATES: Final = ("active", "offered", "reserved")
MAX_FENCED_PATHS: Final = 20000


def _record_path(fence_dir: Path, toplevel: str) -> Path:
    return fence_dir / f"{hashlib.sha256(os.path.realpath(toplevel).encode()).hexdigest()[:16]}.json"


def _listed_files(toplevel: str) -> list[str]:
    """Tracked plus untracked (not ignored) files, repo-relative."""
    try:
        res = subprocess.run(  # noqa: S603,S607
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=toplevel,
            capture_output=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    return sorted({p for p in res.stdout.decode("utf-8", "replace").split("\0") if p})


def _literal_prefix(glob: str) -> str:
    segs: list[str] = []
    for seg in glob.strip("/").split("/"):
        if any(ch in seg for ch in "*?[{"):
            break
        segs.append(seg)
    return "/".join(segs)


def zone_paths(
    toplevel: str, zone: Mapping[str, Any], *, case_insensitive: bool, files: Sequence[str] | None = None
) -> tuple[list[str], list[str]]:
    """``(files, directories)`` of one zone inside a checkout (existing paths only)."""
    includes = [str(g) for g in zone.get("include_globs") or ()]
    excludes = [str(g) for g in zone.get("exclude_globs") or ()]
    listed = list(files) if files is not None else _listed_files(toplevel)

    def inside(rel: str) -> bool:
        if not any(G.glob_match(g, rel, case_insensitive=case_insensitive) for g in includes):
            return False
        return not any(G.glob_match(g, rel, case_insensitive=case_insensitive) for g in excludes)

    matched = [p for p in listed if inside(p)]
    dirs: set[str] = set()
    for g in includes:
        prefix = _literal_prefix(g)
        if not prefix or not os.path.isdir(os.path.join(toplevel, prefix)):
            continue
        if not g.rstrip("/").endswith("**") and prefix != g.strip("/"):
            continue  # only whole-directory globs (dir/** or dir) fence directories
        for root, subdirs, _files in os.walk(os.path.join(toplevel, prefix)):
            rel = os.path.relpath(root, toplevel).replace(os.sep, "/")
            if rel.split("/")[0] == ".git":
                continue
            if any(G.glob_match(x, rel + "/_", case_insensitive=case_insensitive) for x in excludes):
                subdirs[:] = []
                continue
            dirs.add(rel)
    return matched, sorted(dirs)


@dataclass
class FenceResult:
    fenced: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Fence:
    """Applies and restores the fence of one host."""

    def __init__(self, fence_dir: Path) -> None:
        self.dir = fence_dir

    def _load(self, toplevel: str) -> dict[str, Any]:
        try:
            data = json.loads(_record_path(self.dir, toplevel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"toplevel": os.path.realpath(toplevel), "entries": {}}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"toplevel": os.path.realpath(toplevel), "entries": {}}
        return data

    def _save(self, toplevel: str, record: Mapping[str, Any]) -> None:
        path = _record_path(self.dir, toplevel)
        if record.get("entries"):
            atomic_write_json(path, dict(record))
        else:
            try:
                path.unlink()
            except OSError:
                pass

    def fenced(self, toplevel: str) -> dict[str, Any]:
        entries: dict[str, Any] = self._load(toplevel)["entries"]
        return entries

    def apply(self, toplevel: str, zones: Iterable[Mapping[str, Any]], *, case_insensitive: bool) -> FenceResult:
        """Make exactly the given zones read-only in ``toplevel``; restore anything fenced before but not now."""
        top = os.path.realpath(toplevel)
        record = self._load(top)
        entries: dict[str, Any] = record["entries"]
        listed = _listed_files(top)
        want: dict[str, str] = {}
        for zone in zones:
            files, dirs = zone_paths(top, zone, case_insensitive=case_insensitive, files=listed)
            for f in files:
                want.setdefault(f, "file")
            for d in dirs:
                want.setdefault(d, "dir")
            if len(want) > MAX_FENCED_PATHS:
                break
        result = FenceResult()
        for rel in sorted(set(entries) - set(want), key=lambda r: -r.count("/")):
            if self._restore_one(top, rel, entries.pop(rel), result):
                result.restored.append(rel)
        # files first, then directories deepest-first so a directory is closed after its contents
        order = sorted(want, key=lambda r: (want[r] == "dir", -r.count("/")))
        for rel in order:
            if rel in entries:
                continue
            path = os.path.join(top, rel)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue  # never chmod through a symlink
            mode = stat.S_IMODE(st.st_mode)
            if mode & WRITE_BITS == 0:
                continue  # already read-only by the user's choice; nothing to restore later
            try:
                os.chmod(path, mode & ~WRITE_BITS)
            except OSError as e:
                result.errors.append(f"{rel}: {e.__class__.__name__}")
                continue
            entries[rel] = {"mode": mode, "kind": want[rel]}
            result.fenced.append(rel)
        record["toplevel"] = top
        self._save(top, record)
        return result

    def _restore_one(self, top: str, rel: str, entry: Mapping[str, Any], result: FenceResult) -> bool:
        path = os.path.join(top, rel)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return True
        except OSError as e:
            result.errors.append(f"{rel}: {e.__class__.__name__}")
            return False
        if stat.S_ISLNK(st.st_mode):
            return True
        try:
            os.chmod(path, int(entry.get("mode", 0o644)))
        except OSError as e:
            result.errors.append(f"{rel}: {e.__class__.__name__}")
            return False
        return True

    def restore(self, toplevel: str) -> FenceResult:
        """Restore every recorded mode of one checkout (release, session end)."""
        top = os.path.realpath(toplevel)
        record = self._load(top)
        entries: dict[str, Any] = record["entries"]
        result = FenceResult()
        # directories first (shallowest), so files inside them can be restored
        for rel in sorted(entries, key=lambda r: (entries[r].get("kind") != "dir", r.count("/"))):
            if self._restore_one(top, rel, entries[rel], result):
                result.restored.append(rel)
        for rel in result.restored:
            entries.pop(rel, None)
        self._save(top, record)
        return result

    def sweep(self, keep: Iterable[str] = ()) -> FenceResult:
        """At crewd start: restore every recorded checkout except those still fenced for a live session."""
        keep_set = {os.path.realpath(k) for k in keep}
        total = FenceResult()
        if not self.dir.is_dir():
            return total
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            top = str(data.get("toplevel") or "")
            if not top or top in keep_set:
                continue
            res = self.restore(top)
            total.restored.extend(f"{top}:{r}" for r in res.restored)
            total.errors.extend(res.errors)
        return total


def zones_to_fence(snapshot: Mapping[str, Any], advisory_session_id: str) -> list[Mapping[str, Any]]:
    """Zones another session holds exclusive (active/offered) or reserved, seen from one advisory session."""
    zones = {z.get("id"): z for z in snapshot.get("zones") or () if not z.get("builtin")}
    out: dict[str, Mapping[str, Any]] = {}
    for c in snapshot.get("claims") or ():
        if c.get("holder_session_id") == advisory_session_id and c.get("state") != "reserved":
            continue
        if c.get("state") not in BLOCKING_STATES:
            continue
        if c.get("state") == "reserved":
            if c.get("reserved_for") == advisory_session_id or (
                c.get("holder_session_id") == advisory_session_id and c.get("reserved_for") in (None, advisory_session_id)
            ):
                continue
        elif c.get("mode") != "exclusive":
            continue
        zone = zones.get(c.get("zone_id"))
        if zone is not None:
            out[str(zone.get("id"))] = zone
    return list(out.values())
