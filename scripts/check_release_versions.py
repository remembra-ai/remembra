#!/usr/bin/env python3
"""Fail a release when the versions that ship together disagree.

One tag publishes three things that name a version: the ``remembra`` wheel
(pyproject.toml and ``remembra.__version__``), the ``remembra-mcp`` launcher
(packages/remembra-mcp/pyproject.toml, which pins ``remembra[mcp]==``) and the
MCP Registry entry (server.json, top level and package). A registry client
runs ``uvx remembra-mcp==<server.json version>``, so a mismatch ships an entry
that installs the wrong server or none.

    python scripts/check_release_versions.py            # all files agree
    python scripts/check_release_versions.py v0.16.0    # ... and match the tag

Exit 0 when everything agrees, 1 with one line per problem otherwise.
Stdlib only (tomllib), so it runs before any install.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = "remembra-mcp"


def collect(root: Path = ROOT) -> dict[str, str | None]:
    """Every version string that must be equal, keyed by where it comes from."""
    found: dict[str, str | None] = {}
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    found["pyproject.toml"] = project.get("version")

    init = (root / "src" / "remembra" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init, re.MULTILINE)
    found["src/remembra/__init__.py"] = match.group(1) if match else None

    launcher = tomllib.loads((root / "packages" / LAUNCHER / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    found[f"packages/{LAUNCHER}/pyproject.toml"] = launcher.get("version")
    pins = [d for d in launcher.get("dependencies", []) if re.match(r"^remembra\s*\[", d)]
    pin = re.search(r"==\s*([^\s,;]+)", pins[0]) if len(pins) == 1 else None
    found[f"packages/{LAUNCHER}/pyproject.toml remembra[mcp] pin"] = pin.group(1) if pin else None

    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    found["server.json version"] = server.get("version")
    packages = server.get("packages") or []
    launcher_entries = [p for p in packages if p.get("registryType") == "pypi" and p.get("identifier") == LAUNCHER]
    found["server.json packages[remembra-mcp].version"] = launcher_entries[0].get("version") if launcher_entries else None
    return found


def problems(found: dict[str, str | None], tag: str | None = None) -> list[str]:
    out = [f"{where}: no version found" for where, value in found.items() if not value]
    values = {value for value in found.values() if value}
    if len(values) > 1:
        out.append("versions differ: " + ", ".join(f"{where}={value}" for where, value in found.items()))
    if tag is not None:
        wanted = tag[1:] if tag.startswith("v") else tag
        if values and values != {wanted}:
            out.append(f"tag {tag} does not match the files ({', '.join(sorted(values))})")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    tag = args[0] if args else None
    found = collect()
    issues = problems(found, tag)
    for issue in issues:
        print(f"release version check: {issue}", file=sys.stderr)
    if not issues:
        print(f"release version check: all {len(found)} version fields are {next(iter(found.values()))}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
