"""``remembra_help``: quotes from the bundled pack (a verbatim copy of two public docs pages), facts from code,
the governing page for sensitive topics, and "can't confirm" for everything else."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from remembra.cloud.plans import FOUNDING_ANNUAL_PRICE_CENTS, PLANS
from remembra.marshal import knowledge, tools

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = (
    "bugs/",
    "feedback/",
    "competitive-analysis-2026",
    "DEPLOYING",
    "DEPLOYMENT",
    "quickstart",
    "crew.html",
    "server-card",
)


def test_the_pack_is_the_public_docs_verbatim() -> None:
    assert sorted(p.name for p in knowledge.PACK_DIR.glob("*")) == ["plans-and-credits.md", "relay.md"]
    assert (knowledge.PACK_DIR / "relay.md").read_text() == (ROOT / "docs" / "guides" / "relay.md").read_text()
    assert (knowledge.PACK_DIR / "plans-and-credits.md").read_text() == (
        ROOT / "docs" / "reference" / "plans-and-credits.md"
    ).read_text()
    check = subprocess.run([sys.executable, "scripts/sync_marshal_pack.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_the_pack_holds_only_published_pages() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text().replace("!!python/name:", "tag-"))
    nav = str(config["nav"])
    assert "guides/relay.md" in nav and "reference/plans-and-credits.md" in nav
    excluded = config.get("exclude_docs") or ""
    for page in ("guides/relay.md", "reference/plans-and-credits.md"):
        assert page not in excluded
    for name in knowledge.PAGES:
        text = (knowledge.PACK_DIR / name).read_text()
        for bad in EXCLUDED:
            assert f"/{bad}" not in text and f"{bad}.md" not in text, (name, bad)


def test_facts_are_the_plan_catalog() -> None:
    facts = knowledge.facts()
    by_tier = {p["tier"]: p for p in facts["plans"]}
    assert set(by_tier) == {t.value for t in PLANS}
    assert "free" in by_tier  # /billing/plans leaves Free out; the code does not
    for tier, limits in PLANS.items():
        row = by_tier[tier.value]
        assert row["api_keys"] == limits.max_api_keys
        assert row["relay_burst_per_minute"] == limits.relay_burst_per_min
        assert row["price_monthly_cents"] == limits.price_monthly_cents
        assert row["projects"] == ("unlimited" if limits.max_projects == 1000 else limits.max_projects)
    assert by_tier["solo"]["projects"] == "unlimited" and by_tier["free"]["projects"] == 3
    assert facts["founding"]["annual_price_cents"] == FOUNDING_ANNUAL_PRICE_CENTS
    assert facts["crew"] == "Crew mode is planned for 0.17.0. It isn't available yet."
    assert facts["windows"] == "Windows setup is not tested."
    assert facts["source"].startswith("package (remembra ")


@pytest.mark.parametrize(
    ("question", "title", "anchor"),
    [
        ("How do I uninstall Remembra?", "Uninstall", "#uninstall"),
        ("Codex hooks are not running, how do I trust them?", "Codex trust and other agent notes", "#codex-trust"),
        ("my handoffs are queued and not sent", "If the server cannot be reached", "#if-the-server-cannot-be-reached"),
        ("how do I run the doctor", "Doctor", "#doctor"),
        ("which project does a repository use", "Which project a repository uses", "#which-project-a-repository-uses"),
        ("what are smart credits", "Smart credits", "#smart-credits"),
        ("what does the brief look like", "Reading the brief", "#reading-the-brief"),
    ],
)
def test_answers_are_quoted_sections(question: str, title: str, anchor: str) -> None:
    result = knowledge.lookup(question)
    assert result["answer_status"] == "answered"
    top = result["sections"][0]
    assert top["title"] == title and top["url"].endswith(anchor)
    page = knowledge.PACK_DIR / ("relay.md" if "relay" in top["url"] else "plans-and-credits.md")
    body = top["text"].removesuffix("\n…")
    assert body in page.read_text()  # verbatim, never paraphrased
    assert len(result["sections"]) <= knowledge.MAX_SECTIONS


@pytest.mark.parametrize(
    ("question", "page"),
    [
        ("Can I get a refund?", "https://remembra.dev/refunds"),
        ("Are you SOC 2 certified?", "https://remembra.dev/security"),
        ("Is my data encrypted at rest?", "https://remembra.dev/security"),
        ("Do you have an EU region?", "https://remembra.dev/privacy"),
        ("how long is data retention", "https://remembra.dev/privacy"),
        ("who are your subprocessors", "https://remembra.dev/subprocessors"),
        ("do you train on my data", "https://remembra.dev/privacy"),
        ("what is your uptime SLA", "https://remembra.dev/security"),
        ("is it GDPR compliant", "https://remembra.dev/privacy"),
    ],
)
def test_sensitive_topics_get_the_page_not_a_quote(question: str, page: str) -> None:
    result = knowledge.lookup(question)
    assert result["answer_status"] == "read_the_page" and page in result["pages"] and result["sections"] == []
    payload = tools.help_payload(question)
    assert "Quote that page, or say you can't confirm." in payload["rendered"]


@pytest.mark.parametrize("question", ["What is the capital of France?", "tell me a joke", "", "??"])
def test_everything_else_is_cant_confirm(question: str) -> None:
    result = knowledge.lookup(question)
    assert result == {"answer_status": "cant_confirm", "sections": [], "pages": ["https://remembra.dev/contact"], "facts": []}
    assert "Can't confirm that from Remembra's docs." in tools.help_payload(question)["rendered"]


def test_code_facts_and_plan_questions() -> None:
    crew = tools.help_payload("Is crew mode available yet?")
    assert crew["answer_status"] == "answered" and crew["facts"] == ["Crew mode is planned for 0.17.0. It isn't available yet."]
    windows = tools.help_payload("Does it work on Windows?")
    assert "Windows setup is not tested." in windows["facts"]
    machines = tools.help_payload("Does Free cover 4 machines?")
    assert machines["answer_status"] == "answered" and machines["sections"][0]["title"] == "Plans"
    assert {p["tier"] for p in machines["plan_facts"]} >= {"free", "solo"}
    assert machines["pricing_page"] == "https://remembra.dev/pricing"
    rendered = machines["rendered"]
    assert "| **Free** | $0, no card |" in rendered  # the page's own table
    assert rendered.rstrip().endswith("Nothing was changed.")
    assert "No model wrote this." in rendered


def test_slugs_match_mkdocs_toc() -> None:
    assert knowledge.slugify("If the server cannot be reached") == "if-the-server-cannot-be-reached"
    assert knowledge.slugify("When Claude Code hits a limit") == "when-claude-code-hits-a-limit"
    assert knowledge.slugify("`remembra-relay` and **you**") == "remembra-relay-and-you"


def test_every_doc_link_in_a_finding_is_a_real_anchor() -> None:
    from remembra.marshal import rules

    anchors = {s.url for s in knowledge.pack_sections()} | set(knowledge.PAGES.values())
    links = [getattr(rules, name) for name in dir(rules) if name.startswith("DOC")]
    assert len(links) >= 7
    for link in links:
        assert "https://" + link in anchors, link
