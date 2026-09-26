"""The README is the first page most people read: hold its claims to the code.

* Its first screen names Remembra Relay and shows the same three install lines
  as the website.
* The example brief is exactly what ``render_last_session`` prints for the
  facts described next to it, so the example cannot drift from the product.
* The MCP tool count and list match the tools the MCP server registers.
* Benchmark scores (README and docs/benchmarks.md) state the sample size and
  the overall result from the one result file in benchmarks/.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from remembra.relay import handoff as h

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
MCP_SERVER = (ROOT / "src" / "remembra" / "mcp" / "server.py").read_text()

INSTALL = [
    "pipx install 'remembra[mcp]'",
    "remembra-install --all --api-key <your-key>",
    "remembra-relay connect",
]


def _first_screen() -> str:
    return README[: README.index("## How it works")]


def test_first_screen_leads_with_relay_install_and_the_contradiction() -> None:
    first = _first_screen()
    assert '<h1 align="center">Remembra Relay</h1>' in first
    block = re.search(r"## Install\n\n```bash\n(.*?)\n```", first, re.S)
    assert block is not None
    assert block.group(1).splitlines() == INSTALL
    assert "the agent's summary contradicts these recorded facts" in first


def test_install_lines_match_the_website() -> None:
    home = (ROOT / "landing" / "index.html").read_text()
    copied = re.search(r'data-copy="([^"]*)"', home)
    assert copied is not None
    assert copied.group(1).replace("&#10;", "\n").replace("&lt;", "<").replace("&gt;", ">").split("\n") == INSTALL


def _example_brief() -> str:
    """render_last_session for the README's example: Claude Code says 'pushed', git has 2 unpushed commits."""
    facts = {
        "branch": "feat/pdf-export",
        "head_commit": "a41f2c9e0b1d",
        "commits": [
            {"sha": "a41f2c9e0b1d", "subject": "Add PDF export for invoices"},
            {"sha": "77c01aa3f2e9", "subject": "Embed fonts in exported PDFs"},
        ],
        "files_changed": ["invoices/pdf.py", "invoices/fonts.py", "tests/test_pdf.py"],
        "unpushed_commits": 2,
        "upstream": "origin/feat/pdf-export",
        "tests": [{"cmd": "pytest tests/test_pdf.py", "passed": True, "summary": "12 passed"}],
    }
    grounding = h.check_summary_grounding("PDF export is done and pushed. Tests pass.", facts)
    assert grounding["status"] == "contradicted"
    sections = h.build_sections(facts)
    relay = {
        "agent_id": "claude-code",
        "agent_verified": True,
        "branch": facts["branch"],
        "head_commit": facts["head_commit"],
        "grounding": grounding,
        "facts_source": "relay-cli:git+transcript",
        **{k: sections[k] for k in ("done", "not_done", "failing", "next", "next_source")},
    }
    now = datetime(2026, 9, 25, 15, 0, tzinfo=UTC)
    handoff = {
        "id": "3c9e",
        "created_at": (now - timedelta(minutes=12)).isoformat(),
        "source": h.RELAY_ROW_SOURCE,
        "trust_score": 1.0,
        "metadata": {"source": "relay", "relay": relay},
    }
    return h.render_last_session(handoff, now)


def test_readme_example_brief_is_what_the_relay_prints() -> None:
    m = re.search(r"<!-- readme-brief:[^>]*-->\n```text\n(.*?)\n```", README, re.S)
    assert m is not None, "the README's example brief lost its marker"
    assert m.group(1) == _example_brief()


def test_readme_names_every_mcp_tool_and_the_real_count() -> None:
    registered = []
    lines = MCP_SERVER.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("@mcp.tool"):
            name = next(
                re.match(r"\s*(?:async\s+)?def\s+(\w+)", ln) for ln in lines[i : i + 60] if re.match(r"\s*(?:async\s+)?def\s", ln)
            )
            registered.append(name.group(1))
    assert len(registered) == 21
    assert f"**Available tools ({len(registered)}):**" in README
    for tool in registered:
        assert f"`{tool}`" in README, tool
    assert "11 tools" not in README.lower() and "11 total" not in README.lower()


def test_readme_drops_the_claims_research_found_wrong() -> None:
    lowered = README.lower()
    assert "control plane" not in lowered
    assert "not production-ready" not in lowered
    assert "discord.gg/Bzv3JshRa3" not in README
    for claim in ("first agent continuity", "no competitor", "zero external calls", "first-mover", "fully supported"):
        assert claim not in lowered, claim
    assert not re.search(r"\bsigned\b|can.t be forged", README, re.I)


BENCH = ROOT / "benchmarks" / "results_20260307_040346_summary.json"


def _benchmark_problems(text: str, run: dict) -> list[str]:
    """Sections that claim a 100% score without the sample size and the overall result beside it.

    The README once said "100%" on "the LoCoMo benchmark" for 1 of 10 conversations,
    minus the adversarial category it scored 0 on.
    """
    adversarial = run["categories"]["adversarial"]["count"]
    must = [
        "1 of the 10",
        f"{round(run['overall_accuracy'])}%",
        f"{run['total_questions']} questions",
        str(adversarial),
        str(run["total_questions"] - adversarial),
        BENCH.name,
    ]
    problems = []
    for section in re.split(r"\n(?=#{1,3} )", text):
        if "100%" in section:
            problems += [f"{section.splitlines()[0]}: missing {m!r}" for m in must if m not in section]
    if "the standard academic benchmark" in text:
        problems.append("calls one conversation 'the standard academic benchmark'")
    return problems


def test_benchmark_claims_match_the_only_result_file() -> None:
    run = json.loads(BENCH.read_text())
    assert run["conversations_ingested"] == 1 and run["total_questions"] == 199  # what the wording says
    docs = (ROOT / "docs" / "benchmarks.md").read_text()
    for name, text in (("README.md", README), ("docs/benchmarks.md", docs)):
        assert "100%" in text, f"{name}: the benchmark section is gone; retire this test with it"
        assert _benchmark_problems(text, run) == [], name


def test_benchmark_check_catches_an_unqualified_score() -> None:
    run = json.loads(BENCH.read_text())
    old = "## Benchmark Results\n\nTested on LoCoMo, the standard academic benchmark.\n\n| Overall | **100%** | 152 |\n"
    problems = _benchmark_problems(old, run)
    assert any("1 of the 10" in p for p in problems) and any("76%" in p for p in problems)
    assert any("standard academic" in p for p in problems)
