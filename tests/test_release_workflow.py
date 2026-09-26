"""Supply-chain rules for .github/workflows (R-31).

- Every ``uses:`` is pinned to a full 40-character commit SHA (tags can be
  repointed: tj-actions/changed-files, CVE-2025-30066), and Dependabot is
  configured to propose updates for those pins.
- The release publishes to PyPI with trusted publishing (no password input),
  behind the ``release`` environment, and only the publish jobs can mint an
  OIDC token.
- The same tag publishes the MCP Registry entry after PyPI.

PyPI trusted publishing: https://docs.pypi.org/trusted-publishers/using-a-publisher/
MCP Registry GitHub Actions publishing:
https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/github-actions.mdx
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on:` as boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _steps(workflow: dict[str, Any]):
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


def test_every_action_is_pinned_to_a_commit_sha():
    assert WORKFLOWS, "no workflows found"
    unpinned = []
    for path in WORKFLOWS:
        for job, step in _steps(_load(path)):
            uses = step.get("uses")
            if uses and not uses.startswith("./") and not SHA_PIN.match(uses):
                unpinned.append(f"{path.name}:{job}: {uses}")
    assert unpinned == []


def test_the_backlog_grep_finds_no_tag_pins():
    """R-31 acceptance: `grep -E 'uses: [^@]+@(v[0-9]|release/)' .github/workflows/*.yml` returns nothing."""
    pattern = re.compile(r"uses: [^@]+@(v[0-9]|release/)")
    hits = [f"{p.name}:{n}" for p in WORKFLOWS for n, line in enumerate(p.read_text().splitlines(), 1) if pattern.search(line)]
    assert hits == []


def test_each_pin_names_its_version_in_a_comment():
    for path in WORKFLOWS:
        for line in path.read_text().splitlines():
            if re.search(r"uses: [^./][^@]*@[0-9a-f]{40}", line):
                assert re.search(r"@[0-9a-f]{40} # v\d+(\.\d+)*$", line), f"{path.name}: {line.strip()}"


def test_dependabot_updates_the_action_pins():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"]: u for u in config["updates"]}
    assert ecosystems["github-actions"]["directory"] == "/"


def test_release_has_no_stored_pypi_password():
    text = RELEASE.read_text()
    assert "PYPI_API_TOKEN" not in text and "TWINE_PASSWORD" not in text
    for _, step in _steps(_load(RELEASE)):
        if "gh-action-pypi-publish" in str(step.get("uses")):
            assert "password" not in (step.get("with") or {})
            assert (step.get("with") or {}).get("attestations") is True


def test_only_the_publish_jobs_hold_oidc_and_they_are_gated():
    workflow = _load(RELEASE)
    assert workflow["permissions"] == {}, "default token must have no permissions"
    jobs = workflow["jobs"]
    oidc = {name for name, job in jobs.items() if (job.get("permissions") or {}).get("id-token") == "write"}
    assert oidc == {"publish-pypi", "publish-mcp-registry"}
    for name, job in jobs.items():
        assert "permissions" in job, f"{name} must declare its permissions"
    pypi = jobs["publish-pypi"]
    assert pypi["environment"]["name"] == "release"
    assert pypi["needs"] == "build"
    assert set(pypi["permissions"]) == {"id-token"}
    # The registry job runs only after the approved PyPI upload succeeded.
    assert jobs["publish-mcp-registry"]["needs"] == "publish-pypi"
    # Docker Hub still uses a token; it is only reachable after the same approval.
    assert jobs["docker"]["environment"]["name"] == "release"
    writers = {name for name, job in jobs.items() if (job.get("permissions") or {}).get("contents") == "write"}
    assert writers == {"github-release"}


def test_one_tag_publishes_pypi_then_the_mcp_registry():
    workflow = _load(RELEASE)
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    build = "\n".join(str(s.get("run", "")) for _, s in _steps({"jobs": {"b": workflow["jobs"]["build"]}}))
    assert 'check_release_versions.py "$GITHUB_REF_NAME"' in build
    assert "python -m build --outdir dist packages/remembra-mcp" in build
    assert "mcp_stdio_check.py" in build and "remembra-mcp==${version}" in build
    registry = "\n".join(str(s.get("run", "")) for _, s in _steps({"jobs": {"r": workflow["jobs"]["publish-mcp-registry"]}}))
    assert "sha256sum --check" in registry
    assert registry.index("mcp-publisher login github-oidc") < registry.index("mcp-publisher publish server.json")
    assert "pypi.org/pypi/remembra-mcp/${version}/json" in registry


def test_checkouts_do_not_leave_credentials_behind():
    for _, step in _steps(_load(RELEASE)):
        if "actions/checkout" in str(step.get("uses")):
            assert (step.get("with") or {}).get("persist-credentials") is False


def test_docker_image_carries_provenance_and_sbom():
    for _, step in _steps(_load(RELEASE)):
        if "docker/build-push-action" in str(step.get("uses")):
            assert step["with"]["provenance"] == "mode=max"
            assert step["with"]["sbom"] is True
