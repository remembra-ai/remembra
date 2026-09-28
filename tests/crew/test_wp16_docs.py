"""WP-16: the Crew mode docs describe the code that ships, not a plan.

``docs/relay/crew.md`` and ``docs/OPERATIONS.md`` are read the way a reader follows them: the
zones.yml example is compiled by the server's parser, every ``remembra-crew`` command and option
is parsed by the real CLI parsers, every ``python -m remembra.storage.snapshot`` command by the
snapshot parser, every MCP tool named exists, and every environment variable named is read
somewhere in the code or the entrypoint.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from remembra.crew import schemas as S
from remembra.crew.policy import parse_zones_yaml
from remembra.relay.crew import cli, install, verify
from remembra.storage import snapshot

ROOT = Path(__file__).resolve().parents[2]
CREW_DOC = ROOT / "docs" / "relay" / "crew.md"
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
DOCS = (CREW_DOC, OPERATIONS)


def _code_blocks(text: str, lang: str | None = None) -> list[str]:
    blocks = re.findall(r"^( *)```(\w*)\n(.*?)^\1```", text, flags=re.S | re.M)
    out = []
    for indent, block_lang, body in blocks:
        if lang is None or block_lang == lang:
            out.append("\n".join(line[len(indent) :] for line in body.splitlines()))
    return out


def _command_lines(prefix: str) -> list[list[str]]:
    """Every command line starting with ``prefix`` in a shell block or an inline code span of the docs."""
    found: list[list[str]] = []
    for doc in DOCS:
        text = doc.read_text()
        candidates = [line.split("#", 1)[0] for block in _code_blocks(text) for line in block.splitlines()]
        candidates += re.findall(r"`([^`\n]+)`", text)
        for line in candidates:
            line = line.strip().removeprefix('docker exec "$CTR"').strip()  # run inside the API container
            if not line.startswith(prefix):
                continue
            # table notation: [optional parts] are dropped, a|b alternatives keep the first, <x> is a value
            line = re.sub(r"\[[^\]]*\]", "", line).replace("\\|", "|")
            line = re.sub(r"(\S+?)\|\S+", r"\1", line)
            line = re.sub(r"<[^>]*>", "X", line).replace("…", "x")
            found.append(shlex.split(line)[len(prefix.split()) :])
    return found


def test_zones_example_compiles_with_the_server_parser() -> None:
    blocks = _code_blocks(CREW_DOC.read_text(), "yaml")
    assert len(blocks) == 1
    policy = parse_zones_yaml(blocks[0])
    assert policy.slugs == ["pos", "payroll", "reports", "database"]
    assert policy.zone("pos").fail_closed is True  # type: ignore[union-attr]
    assert policy.zone("database").commands == ("supabase db push *",)  # type: ignore[union-attr]
    assert dict(policy.commons) == {"package.json": "plain", "supabase/migrations/**": "append_only"}


def test_every_remembra_crew_command_in_the_docs_parses() -> None:
    lines = _command_lines("remembra-crew")
    assert len(lines) >= 10
    seen = set()
    for argv in lines:
        argv = [a for a in argv if a not in {"|", "\\|"}]
        if not argv:
            continue
        sub = argv[0]
        seen.add(sub)
        if sub == "connect":
            install.build_parser().parse_args(argv[1:])
        elif sub == "verify":
            verify.build_parser().parse_args(argv[1:])
        else:
            assert sub in cli.COMMANDS, sub
    assert {"connect", "verify", "doctor", "status", "claim", "adopt", "report", "zones"} <= seen


@pytest.mark.parametrize(
    "argv",
    [
        ["claim", "pos", "--wait", "30"],
        ["release", "pos", "--baton"],
        ["adopt", "T-3"],
        ["task", "start", "T-3"],
        ["report", "T-3", "--done", "a", "--not-done", "b", "--failing", "c", "--next", "d"],
        ["say", "hi", "--to", "@codex-1", "--wait", "30"],
        ["watch"],
        ["zones", "push"],
        ["whereami"],
        ["checkpoint"],
    ],
)
def test_everyday_command_table_matches_the_cli(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args is not None


def test_every_snapshot_command_in_the_docs_parses() -> None:
    lines = _command_lines("python -m remembra.storage.snapshot")
    assert {a[0] for a in lines} == {"create", "verify", "restore"}
    for argv in lines:
        snapshot._parser().parse_args(argv)


def test_mcp_tools_named_in_the_docs_exist() -> None:
    named = set(re.findall(r"`(crew_[a-z_]+)`", CREW_DOC.read_text()))
    shipped = {t.name for t in S.MCP_TOOLS}
    assert named == shipped


def test_environment_variables_named_in_the_docs_are_read_by_the_code() -> None:
    sources = "\n".join(p.read_text() for p in (ROOT / "src" / "remembra").rglob("*.py"))
    sources += (ROOT / "scripts" / "cloud-entrypoint.sh").read_text()
    from remembra.config import Settings

    settings_env = {f"REMEMBRA_{f.upper()}" for f in Settings.model_fields}
    sections = {
        "crew.md": (None, None),
        "OPERATIONS.md": ("## Scheduled snapshots", "## Health, readiness, metrics"),
        "configuration.md": ("## Crew mode", "## Embeddings"),
    }
    for doc in (*DOCS, ROOT / "docs" / "reference" / "configuration.md"):
        text = doc.read_text()
        start, end = sections[doc.name]
        if start and end:
            text = text[text.index(start) : text.index(end)]
        for name in sorted(set(re.findall(r"\b((?:REMEMBRA|LITESTREAM)_[A-Z0-9_]*[A-Z0-9])\b", text))):
            if name in {"REMEMBRA_BYPASS"}:  # consumed by the vendored gate from a human-typed command
                assert "REMEMBRA_BYPASS" in sources
                continue
            if name in settings_env:
                continue
            assert f'"{name}"' in sources or f"${{{name}" in sources or f"${name}" in sources, (doc.name, name)


def test_flag_default_is_documented_as_off() -> None:
    text = CREW_DOC.read_text()
    assert "| `REMEMBRA_CREW_MODE` | `false` |" in text
    assert "ENV REMEMBRA_CREW_MODE=false" in (ROOT / "Dockerfile.cloud").read_text()


def test_trust_model_states_the_honest_limit() -> None:
    text = CREW_DOC.read_text()
    trust = text[text.index("## Trust model") : text.index("## Before you start")]
    assert "coordinate cooperative agents" in trust
    assert "cannot stop a determined" in trust
    assert "It cannot stop an agent deliberately working around it on" in trust
    assert "not in this release" in trust  # the server-side required check is L1


def test_docs_are_in_the_site_navigation() -> None:
    assert "relay/crew.md" in (ROOT / "mkdocs.yml").read_text()
    operations = OPERATIONS.read_text()
    for heading in ("## Scheduled snapshots", "## Crew mode", "### Turning it on", "### Rollback plan"):
        assert heading in operations
    assert not (ROOT / "docs" / "DEPLOYING.md").exists()  # the hosted runbook left the repository (LEAK-5)


def test_scheduled_snapshot_command_keeps_a_bounded_number_and_covers_crew_db() -> None:
    """The documented scheduled task is a real `create` with retention, and says crew.db is in it."""
    operations = OPERATIONS.read_text()
    section = operations[operations.index("## Scheduled snapshots") : operations.index("## Backups (litestream)")]
    command = "python -m remembra.storage.snapshot create --out /data/backups --keep 7"
    assert f"| Command | `{command}` |" in section
    args = snapshot._parser().parse_args(shlex.split(command)[3:])
    assert (args.command, args.out, args.keep) == ("create", "/data/backups", 7)
    assert "`crew.db`" in section and "Coolify" in section and "0 3 * * *" in section


def test_changelog_says_teammates_are_added_through_the_api_only() -> None:
    """Owner decision 14: POST /crews/{id}/members (a dashboard login) is the only way in 0.17.0; no dashboard screen
    adds a teammate, and a teammate's own agents start a crew of their own."""
    section = (ROOT / "CHANGELOG.md").read_text().split("## [0.17.0]", 1)[1].split("\n## [", 1)[0]
    flat = " ".join(section.split())
    assert (
        "The crew owner can add teammates through the API; the dashboard flow and teammates' own agents come in the next"
        " release." in flat
    )
    assert "the crew owner adds people who have joined their team" not in flat
