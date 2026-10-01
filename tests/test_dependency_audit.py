"""DEP-2: the server's own dependencies are watched and audited.

- Dependabot proposes updates for uv.lock (uv, /) and the dashboard's npm lock
  (npm, /dashboard), not only for the Actions pins and the release tools.
- CI runs pip-audit on the exact locked set the production image installs
  (Dockerfile.cloud's extras) with --strict, and fails on any advisory that is
  not in the reviewed allowlist (pip-audit-allowlist.txt).
- The lock carries the fixed releases the 2026-09-26 sweep named.

The audit itself needs the network (PyPI's advisory data), so CI runs it; these
tests pin the wiring so it cannot silently drift.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
ALLOWLIST = ROOT / "pip-audit-allowlist.txt"


def _dependabot() -> dict[tuple[str, str], dict[str, Any]]:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    return {(u["package-ecosystem"], u["directory"]): u for u in config["updates"]}


def _audit_job() -> dict[str, Any]:
    return dict(yaml.safe_load(CI.read_text())["jobs"]["audit"])


def _runs(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


@pytest.mark.parametrize(("ecosystem", "directory"), [("uv", "/"), ("npm", "/dashboard"), ("npm", "/sdk/typescript")])
def test_dependabot_watches_the_server_and_dashboard_locks(ecosystem: str, directory: str) -> None:
    entry = _dependabot()[(ecosystem, directory)]
    assert entry["schedule"]["interval"] == "weekly"
    assert entry["groups"], "updates are grouped into one weekly PR"


def test_ci_audits_and_exercises_both_node_lockfiles() -> None:
    job = yaml.safe_load(CI.read_text())["jobs"]["frontend"]
    assert set(job["strategy"]["matrix"]["path"]) == {"dashboard", "sdk/typescript"}
    assert job["defaults"]["run"]["working-directory"] == "${{ matrix.path }}"
    runs = _runs(job)
    assert "npm ci" in runs and "npm audit --audit-level=low" in runs
    assert "npm test -- --maxWorkers=1" in runs and "npm run build" in runs
    assert job.get("continue-on-error") is not True
    assert all(step.get("continue-on-error") is not True for step in job["steps"])


def test_ci_audits_exactly_what_the_production_image_installs() -> None:
    extras = re.search(r'install-locked-deps\.sh "([^"]+)"', (ROOT / "Dockerfile.cloud").read_text())
    assert extras is not None
    runs = _runs(_audit_job())
    export = re.search(r"uv export --frozen --no-dev --no-emit-project((?: --extra [\w-]+)+)", runs)
    assert export is not None, runs
    assert sorted(export.group(1).split()[1::2]) == sorted(extras.group(1).split())
    assert re.search(r"\| pip-audit --disable-pip -r /dev/stdin --strict", runs)


def test_the_audit_job_installs_its_tools_with_hashes_only() -> None:
    job = _audit_job()
    installs = [line.strip() for line in _runs(job).splitlines() if "pip install" in line]
    assert installs == [
        "python -m pip install --require-hashes --no-deps -r .github/release-requirements.txt",
        "python -m pip install --require-hashes --no-deps -r /tmp/audit-tools.txt",
    ]
    assert "uv export --frozen --only-group audit --no-emit-project -o /tmp/audit-tools.txt" in _runs(job)
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]
    assert any(req.startswith("pip-audit") for req in groups["audit"])
    # It reads the repo and PyPI's advisory data, nothing else.
    assert yaml.safe_load(CI.read_text())["permissions"] == {"contents": "read"}
    assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}


def test_every_allowlisted_advisory_carries_a_reason_and_a_review_date() -> None:
    runs = _runs(_audit_job())
    assert "pip-audit-allowlist.txt" in runs
    for n, line in enumerate(ALLOWLIST.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        assert re.match(r"^[A-Z]+-[A-Za-z0-9-]+ +# .+ \(reviewed \d{4}-\d{2}-\d{2}\)$", line), f"line {n}: {line}"


def test_the_allowlist_parser_in_ci_reads_ids_and_nothing_else(tmp_path: Path) -> None:
    import subprocess

    command = re.search(r"ignores=\$\((.+)\)", _runs(_audit_job()))
    assert command is not None
    sample = tmp_path / "pip-audit-allowlist.txt"
    sample.write_text(
        "# header GHSA-xxxx-not-an-entry\n\nPYSEC-2026-1  # not reachable: x (reviewed 2026-09-26)\n"
        "GHSA-537c-gmf6-5ccf  # y (reviewed 2026-09-26)\n"
    )
    out = subprocess.run(["sh", "-c", command.group(1)], cwd=tmp_path, capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["--ignore-vuln", "PYSEC-2026-1", "--ignore-vuln", "GHSA-537c-gmf6-5ccf"]


@pytest.mark.parametrize(
    ("name", "floor"),
    [("pyjwt", "2.13.0"), ("cryptography", "48.0.1"), ("idna", "3.15"), ("urllib3", "2.7.0")],
)
def test_lock_pins_the_fixed_releases(name: str, floor: str) -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {pkg["name"]: Version(pkg["version"]) for pkg in lock["package"]}
    assert versions[name] >= Version(floor)


def test_the_security_floors_are_declared_where_they_bind() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]
    assert any(req.startswith("PyJWT>=2.13.0") for req in extras["server"])
    assert any(req.startswith("cryptography>=48.0.1") for req in extras["encryption"])
    # Transitive: they bind the lock, not the published package metadata.
    assert {"idna>=3.15", "urllib3>=2.7.0"} <= set(project["tool"]["uv"]["constraint-dependencies"])
