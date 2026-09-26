"""The MCP Registry entry (server.json) and the ``remembra-mcp`` launcher it names.

Registry clients (VS Code's MCP gallery and the directories that copy the
official registry) build the launch command from server.json: for a PyPI
package with runtimeHint ``uvx`` that is ``uvx remembra-mcp==<version>``.
These tests hold server.json to the registry schema
(https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json,
vendored under tests/fixtures/mcp_registry), keep every shipped version in
step (scripts/check_release_versions.py), and start the launcher to check it
answers the MCP ``initialize`` handshake over stdio.

Ownership check: the registry accepts a PyPI package only when its README
contains ``mcp-name: <server name>`` followed by a boundary
(https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx).

``REMEMBRA_REGISTRY_E2E=1`` also builds both wheels and runs the exact uvx
command offline from the local uv cache (needs ``uv`` and a warm cache).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = ROOT / "server.json"
LAUNCHER = ROOT / "packages" / "remembra-mcp"
SCHEMA = ROOT / "tests" / "fixtures" / "mcp_registry" / "server.schema.2025-12-11.json"

sys.path.insert(0, str(ROOT / "scripts"))
import check_release_versions  # noqa: E402
import mcp_stdio_check  # noqa: E402


def _server() -> dict:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))


def _launcher_entry(server: dict) -> dict:
    entries = [p for p in server["packages"] if p.get("identifier") == "remembra-mcp"]
    assert len(entries) == 1, server["packages"]
    return entries[0]


def test_server_json_matches_the_registry_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    server = _server()
    assert server["$schema"] == schema["$id"]
    jsonschema.validate(server, schema)


def test_schema_check_rejects_a_broken_entry():
    """The schema test is not vacuous: a too-long description and a missing transport fail it."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    broken = _server()
    broken["description"] = "x" * 101
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)
    broken = _server()
    del broken["packages"][0]["transport"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(broken, schema)


def test_registry_command_is_the_stdio_launcher_not_the_web_server():
    entry = _launcher_entry(_server())
    assert entry["registryType"] == "pypi"
    assert entry["runtimeHint"] == "uvx"
    assert entry["transport"] == {"type": "stdio"}
    # No arguments: `uvx remembra-mcp==X` is the whole command. The old entry ran
    # `remembra`, which is uvicorn (remembra.main:run) and never speaks MCP.
    assert "packageArguments" not in entry and "runtimeArguments" not in entry
    scripts = tomllib.loads((LAUNCHER / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts == {"remembra-mcp": "remembra_mcp:main"}
    main_scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert main_scripts["remembra-mcp"] == "remembra._launch:mcp"  # same launcher either way
    launcher_src = (LAUNCHER / "src" / "remembra_mcp" / "__init__.py").read_text()
    assert "from remembra._launch import mcp" in launcher_src


def test_environment_variables_declare_the_key_as_secret():
    env = {e["name"]: e for e in _launcher_entry(_server())["environmentVariables"]}
    assert env["REMEMBRA_API_KEY"]["isSecret"] is True
    assert env["REMEMBRA_API_KEY"]["isRequired"] is True
    assert env["REMEMBRA_URL"]["isSecret"] is False
    assert env["REMEMBRA_URL"]["default"] == "https://api.remembra.dev"
    assert all(e.get("isSecret") is False for name, e in env.items() if name != "REMEMBRA_API_KEY")
    # Every variable the entry declares is one the server reads.
    server_src = (ROOT / "src" / "remembra" / "mcp" / "server.py").read_text()
    for name in env:
        assert f'"{name}"' in server_src, name


def test_listing_text_is_about_cross_agent_handoff():
    server = _server()
    assert len(server["description"]) <= 100
    assert "handoff" in server["description"].lower() and "agent" in server["description"].lower()
    assert "11 tools" not in server["description"] and "Memory Layer" not in server.get("title", "")


def test_launcher_readme_proves_ownership_of_the_registry_name():
    server_name = _server()["name"]
    readme = (LAUNCHER / "README.md").read_text(encoding="utf-8")
    assert re.search(rf"mcp-name: {re.escape(server_name)}(?=\s|-->|<|$)", readme)
    project = tomllib.loads((LAUNCHER / "pyproject.toml").read_text())["project"]
    assert project["readme"] == "README.md"
    assert project["name"] == "remembra-mcp"


def test_all_release_versions_agree():
    found = check_release_versions.collect()
    assert check_release_versions.problems(found) == [], found
    version = found["pyproject.toml"]
    assert check_release_versions.problems(found, f"v{version}") == []
    assert check_release_versions.main([f"v{version}"]) == 0


def test_version_check_catches_each_kind_of_drift(tmp_path):
    for rel in ("pyproject.toml", "src/remembra/__init__.py", "packages/remembra-mcp/pyproject.toml", "server.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    good = check_release_versions.collect(tmp_path)
    assert check_release_versions.problems(good) == []
    version = good["pyproject.toml"]
    assert version
    assert check_release_versions.problems(good, "v9.9.9") == [f"tag v9.9.9 does not match the files ({version})"]

    launcher = tmp_path / "packages/remembra-mcp/pyproject.toml"
    launcher.write_text(launcher.read_text().replace(f'"remembra[mcp]=={version}"', '"remembra[mcp]==0.0.1"'))
    issues = check_release_versions.problems(check_release_versions.collect(tmp_path))
    assert len(issues) == 1 and "remembra[mcp] pin=0.0.1" in issues[0]

    launcher.write_text(launcher.read_text().replace('"remembra[mcp]==0.0.1"', '"remembra[mcp]>=0.1"'))
    issues = check_release_versions.problems(check_release_versions.collect(tmp_path))
    assert "packages/remembra-mcp/pyproject.toml remembra[mcp] pin: no version found" in issues

    server = json.loads((tmp_path / "server.json").read_text())
    server["packages"][0]["identifier"] = "remembra"
    (tmp_path / "server.json").write_text(json.dumps(server))
    issues = check_release_versions.problems(check_release_versions.collect(tmp_path))
    assert "server.json packages[remembra-mcp].version: no version found" in issues


def _launcher_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("REMEMBRA_")}
    env.update(
        {
            "PYTHONPATH": os.pathsep.join([str(LAUNCHER / "src"), str(ROOT / "src")]),
            "REMEMBRA_URL": "http://127.0.0.1:9",  # never contacted by initialize / tools/list
            "REMEMBRA_API_KEY": "rem_test_registry_check",
            "REMEMBRA_AGENT_ID": "registry-check",
        }
    )
    return env


def test_launcher_answers_mcp_initialize_over_stdio():
    """The launcher module is what `remembra-mcp` (the launcher's script) runs."""
    pytest.importorskip("mcp")
    result = mcp_stdio_check.check(
        [sys.executable, "-m", "remembra_mcp"],
        timeout=60,
        expect_tools=["session_brief", "close_session"],
        env=_launcher_env(),
    )
    assert result["server"]["name"] == "remembra"


def test_launcher_answers_help_and_version_without_starting_the_server():
    """`uvx remembra-mcp --help` prints usage and exits, like remembra's own script (it once started stdio)."""
    from remembra import __version__

    for flag, expected in (("--help", "usage: remembra-mcp"), ("--version", f"remembra-mcp {__version__}")):
        result = subprocess.run(
            [sys.executable, "-m", "remembra_mcp", flag],
            env=_launcher_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout


def test_stdio_check_fails_on_a_server_that_is_not_mcp():
    """The old registry command started uvicorn; a non-MCP process must fail the check, not hang."""
    with pytest.raises(mcp_stdio_check.CheckError, match="exited"):
        mcp_stdio_check.check([sys.executable, "-c", "print('Uvicorn running on http://0.0.0.0:8787')"], timeout=20)
    assert mcp_stdio_check.main(["--", sys.executable, "-c", "import sys; sys.exit(3)"]) == 1


@pytest.mark.skipif(
    os.environ.get("REMEMBRA_REGISTRY_E2E") != "1", reason="set REMEMBRA_REGISTRY_E2E=1 (builds wheels, runs uvx)"
)
def test_exact_registry_command_from_built_wheels(tmp_path):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv is not installed")
    dist = tmp_path / "dist"
    for project in (ROOT, LAUNCHER):
        subprocess.run([uv, "build", "--wheel", "--out-dir", str(dist), str(project)], check=True, capture_output=True)
    entry = _launcher_entry(_server())
    command = ["uvx", "--offline", "--isolated", "--find-links", str(dist), f"{entry['identifier']}=={entry['version']}"]
    env = _launcher_env()
    env.pop("PYTHONPATH")
    result = mcp_stdio_check.check(command, timeout=180, expect_tools=["session_brief", "close_session"], env=env)
    assert result["server"]["name"] == "remembra"
