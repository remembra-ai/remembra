"""``remembra_help``: answers quoted from a small bundled help pack, or "can't confirm".

The pack (``marshal/pack/``) is a verbatim copy of two published docs pages,
``docs/guides/relay.md`` and ``docs/reference/plans-and-credits.md`` (kept in
sync by ``scripts/sync_marshal_pack.py``; a test fails when they drift).
:func:`lookup` ranks their sections with BM25 (standard library only, no
embeddings, no model) and returns at most three, each with its page URL and
anchor. Nothing is paraphrased or generated: the answer is the page's own
text. Questions about refunds and cancelling, compliance, security and
safety, privacy (who sees the data, selling or sharing it), hosting and
data location, retention and deleting account data, subprocessors, model
training or uptime never get a quote from the pack, in any wording; they
get the governing page to read (``read_the_page``). A data-deletion
question never gets the uninstall steps (they leave the account's
memories). A question the pack doesn't cover gets ``cant_confirm`` and the
contact page.

:func:`facts` is the code truth the pack's prose rests on: plan limits and
list prices from :mod:`remembra.cloud.plans` (Free included; 1000 projects
means unlimited), the adapters' verified status, and the command templates.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

from remembra.marshal import commands as cmd
from remembra.marshal import words

PACK_DIR = Path(__file__).resolve().parent / "pack"
PAGES: dict[str, str] = {
    "relay.md": "https://docs.remembra.dev/guides/relay/",
    "plans-and-credits.md": "https://docs.remembra.dev/reference/plans-and-credits/",
}
READ_PAGES: dict[str, str] = {
    "pricing": "https://remembra.dev/pricing",
    "refunds": "https://remembra.dev/refunds",
    "security": "https://remembra.dev/security",
    "privacy": "https://remembra.dev/privacy",
    "subprocessors": "https://remembra.dev/subprocessors",
}
# What a user's data is called in a question ("my data", "my memories", "my handoffs", "my code").
_DATA = r"(?:data|memor(?:y|ies)|handoffs?|information|info|code|personal \w+)"
# Topics that are never answered from the pack, whole, in any wording: the governing page is linked instead.
# A pack section that shares a few words with such a question does not answer it (a privacy question got
# the brief's format; "delete everything you have about me" got the uninstall steps).
SENSITIVE: dict[str, tuple[str, ...]] = {
    # money: refunds, chargebacks, cancelling a plan
    r"refunds?|money back|reimburs\w*|charge ?backs?"
    r"|cancel\w* (?:my |the |a |your )?(?:subscription|plan|account|membership|billing|payment|trial)": ("refunds", "pricing"),
    # certifications, security
    r"soc ?2|soc\b": ("security",),
    r"penetration|pen ?test": ("security",),
    r"encrypt\w*": ("security",),
    r"secur(?:e|ed|ely|ity)\b": ("security",),
    rf"{_DATA}\b[^.?!]{{0,30}}\bsafe(?:ly|ty)?\b|\bsafe(?:ly|ty)?\b[^.?!]{{0,30}}\b{_DATA}": ("security", "privacy"),
    r"gdpr|hipaa|iso ?27001|pci|complian\w*|certif\w*": ("security", "privacy"),
    r"\bsla\b|uptime|guarantee\w*": ("security",),
    # privacy: the policy, who sees what, selling or sharing it
    rf"privacy|confidential\w*|private {_DATA}|{_DATA}\b[^.?!]{{0,20}}\bprivate\b": ("privacy",),
    # who else sees it (an agent of the user's reading a brief is the product, not this)
    r"who (?:else )?(?:can|could|will|would|has|have|gets?) (?:see|access|read|view)"
    r"|(?:openai|anthropic|google|microsoft|staff|employees?|admins?|anyone|people at remembra|remembra's team)\b"
    r"[^.?!]{0,30}\b(?:see|sees|read|reads|access|accesses|look at)\b": ("privacy", "subprocessors"),
    r"\b(?:sell|sells|sold|selling)\b|third[- ]part(?:y|ies)|advertis\w*"
    r"|(?:share|shares|shared|sharing|give|gives|pass(?:es|ed)?)\b[^.?!]{0,20}"
    r"\b(?:with|to) (?:others|anyone|partners|companies)": ("privacy", "subprocessors"),
    # where it is kept, and for how long
    r"eu region|data (?:location|residency)|where is my data|region|countr(?:y|ies)|data ?cent(?:er|re)s?|which cloud"
    rf"|where (?:are|is|do) (?:your|the|my|you) (?:servers?|{_DATA}|host)|\bhosted (?:in|on|by|where)"
    rf"|(?:servers?|{_DATA})\b[^.?!]{{0,30}}\b(?:hosted|located|stored|kept)\b": ("privacy", "subprocessors"),
    r"retention|retain\w*|delete my (?:data|account)": ("privacy",),
    # deleting what the account holds (never the uninstall steps: they leave the account's memories)
    rf"(?:delet|eras|remov|wip|purg|forget)\w*\b[^.?!]{{0,40}}(?:\bmy (?:account|{_DATA}|everything)"
    rf"|\babout me\b|\byou (?:have|hold|store|keep)\b|\b(?:from|on) your (?:servers?|side|end|database)"
    rf"|\ball (?:of )?my {_DATA}|\baccount data)": ("privacy",),
    r"sub-?processors?": ("subprocessors",),
    # model training, in any wording ("AI training", "train models on", "used to train")
    r"\btrain(?:s|ed|ing|er)?\b": ("privacy",),
}
_SENSITIVE_RE = [(re.compile(rf"\b(?:{pattern})", re.I), pages) for pattern, pages in SENSITIVE.items()]
# Deleting stored data in any wording. The uninstall steps only clear this machine (the key, the queue, the
# log) and leave every memory in the account, so they are never the answer to one of these.
_DELETES_DATA = re.compile(rf"\b(?:delet|eras|wip|purg|forget)\w*\b[^.?!]{{0,40}}\b{_DATA}", re.I)
UNINSTALL_ANCHOR = "uninstall"
_PLAN_WORDS = frozenset(
    re.findall(
        r"\S+",
        "plan plans free solo pro team enterprise price prices cost costs pricing key keys machine machines limit"
        " limits credit credits project projects seat seats founding annual monthly",
    )
)
STOPWORDS = frozenset(
    re.findall(
        r"\S+",
        "a an and are as at be but by can do does for from how i if in is it its me my of on or so that the this to"
        " was what when where which who why will with you your we our there their them then than get got have has"
        " should would could remembra look like tell show give make know want need please help us about just some"
        " any also more one",
    )
)
# Facts the pack's pages don't state in one place, answered from the code (never from a model).
FACT_TOPICS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcrew\b", re.I), "crew"),
    (re.compile(r"\bwindows\b", re.I), "windows"),
)
_PLANS_FALLBACK = "remembra_marshal_cloud_plans"
MIN_SCORE = 1.5
TITLE_BOOST = 1.5
MIN_COVERAGE = 0.5
MAX_SECTIONS = 3
MAX_SECTION_CHARS = 1400
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)(?:\s*\{#([\w-]+)\})?\s*$")


@dataclass(frozen=True)
class Section:
    page: str
    title: str
    anchor: str | None
    text: str
    tokens: tuple[str, ...]
    title_tokens: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        base = PAGES[self.page]
        return f"{base}#{self.anchor}" if self.anchor else base


def slugify(heading: str) -> str:
    """Python-Markdown's toc slug (what mkdocs puts in the page's anchors)."""
    text = re.sub(r"[`*_]", "", heading)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS and len(w) > 1]


def split_sections(page: str, text: str) -> list[Section]:
    sections: list[Section] = []
    title: str = ""
    anchor: str | None = None
    body: list[str] = []
    in_code = False

    def flush() -> None:
        content = "\n".join(body).strip()
        if content or title:
            words = tokens(title) + tokens(content)
            sections.append(Section(page, title, anchor, content, tuple(words), tuple(tokens(title))))

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        match = None if in_code else _HEADING_RE.match(line)
        if match:
            flush()
            level, heading, explicit = match.groups()
            title = heading.strip()
            anchor = explicit or (slugify(title) if len(level) > 1 else None)
            body = []
        else:
            body.append(line)
    flush()
    return sections


@lru_cache(maxsize=1)
def pack_sections() -> tuple[Section, ...]:
    out: list[Section] = []
    for page in PAGES:
        out.extend(split_sections(page, (PACK_DIR / page).read_text(encoding="utf-8")))
    return tuple(out)


def _bm25(query: list[str], sections: tuple[Section, ...], k1: float = 1.5, b: float = 0.75) -> list[tuple[float, Section]]:
    n = len(sections)
    avgdl = sum(len(s.tokens) for s in sections) / max(1, n)
    df: dict[str, int] = {}
    for section in sections:
        for term in set(section.tokens):
            df[term] = df.get(term, 0) + 1
    scored = []
    for section in sections:
        counts: dict[str, int] = {}
        for term in section.tokens:
            counts[term] = counts.get(term, 0) + 1
        score = 0.0
        for term in set(query):
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(section.tokens) / avgdl))
            if term in section.title_tokens:  # a heading that names the question's words counts double
                score += idf * TITLE_BOOST
        scored.append((score, section))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def _excerpt(text: str) -> str:
    if len(text) <= MAX_SECTION_CHARS:
        return text
    cut = text.rfind("\n\n", 0, MAX_SECTION_CHARS)
    return text[: cut if cut > 200 else MAX_SECTION_CHARS].rstrip() + "\n…"


def sensitive_pages(question: str) -> list[str]:
    pages: list[str] = []
    for pattern, names in _SENSITIVE_RE:
        if pattern.search(question):
            pages.extend(READ_PAGES[n] for n in names)
    return list(dict.fromkeys(pages))


def _quote(section: Section) -> dict[str, str]:
    return {"title": section.title, "url": section.url, "text": _excerpt(section.text)}


def lookup(question: str) -> dict[str, Any]:
    """``{answer_status, sections[], pages[], facts[]}``: answered (quoted or from code), read_the_page, or cant_confirm."""
    question = (question or "").strip()[:500]
    pages = sensitive_pages(question)
    if pages:
        return {"answer_status": "read_the_page", "sections": [], "pages": pages, "facts": []}
    known = facts()
    stated = [known[topic] for pattern, topic in FACT_TOPICS if pattern.search(question)]
    query = tokens(question)
    sections = pack_sections()
    chosen: list[Section] = []
    if query:
        ranked = _bm25(query, sections)
        top_score, top = ranked[0]
        vocabulary = set(query)
        coverage = len(vocabulary & set(top.tokens)) / len(vocabulary)
        if top_score >= MIN_SCORE and coverage >= MIN_COVERAGE:
            chosen = [s for score, s in ranked[:MAX_SECTIONS] if score >= top_score * 0.6]
    if not chosen and wants_plan_facts(question):
        chosen = [s for s in sections if s.page == "plans-and-credits.md" and s.title == "Plans"]
    if _DELETES_DATA.search(question) and any(s.anchor == UNINSTALL_ANCHOR for s in chosen):
        return {"answer_status": "read_the_page", "sections": [], "pages": [READ_PAGES["privacy"]], "facts": []}
    if not chosen and not stated:
        return {"answer_status": "cant_confirm", "sections": [], "pages": [cmd.CONTACT_URL], "facts": []}
    return {"answer_status": "answered", "sections": [_quote(s) for s in chosen], "pages": [], "facts": stated}


def plans_module() -> ModuleType:
    """:mod:`remembra.cloud.plans`, also on a base install.

    ``plans.py`` itself needs only the standard library, but importing it as
    ``remembra.cloud.plans`` runs ``remembra/cloud/__init__.py``, which needs
    the server's packages (structlog, pydantic). Without them the file is
    loaded on its own, under a private module name.
    """
    try:
        from remembra.cloud import plans

        return plans
    except ImportError:
        pass
    cached = sys.modules.get(_PLANS_FALLBACK)
    if cached is not None:
        return cached
    path = Path(__file__).resolve().parent.parent / "cloud" / "plans.py"
    spec = importlib.util.spec_from_file_location(_PLANS_FALLBACK, path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file ships in the package
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLANS_FALLBACK] = module  # dataclasses look the module up while the class is built
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_PLANS_FALLBACK, None)
        raise
    return module


def wants_plan_facts(question: str) -> bool:
    return bool(set(re.findall(r"[a-z]+", (question or "").lower())) & _PLAN_WORDS)


def plan_facts() -> list[dict[str, Any]]:
    """Every plan tier's limits and list prices, from the code (Free included)."""
    out = []
    for tier, limits in plans_module().PLANS.items():
        out.append(
            {
                "tier": str(tier.value),
                "name": limits.display_name,
                "api_keys": limits.max_api_keys,
                "projects": "unlimited" if limits.max_projects == 1000 else limits.max_projects,
                "memories": limits.max_memories,
                "smart_credits_per_month": limits.max_smart_credits_per_month,
                "recalls_per_month": limits.max_recalls_per_month,
                "relay_events_per_month_soft_cap": limits.max_relay_events_per_month,
                "relay_burst_per_minute": limits.relay_burst_per_min,
                "unenriched_writes_per_day": limits.max_unenriched_writes_per_day,
                "seats": limits.max_users,
                "per_seat": limits.per_seat,
                "price_monthly_cents": limits.price_monthly_cents,
                "price_annual_cents": limits.price_annual_cents,
            }
        )
    return out


def facts() -> dict[str, Any]:
    from remembra import __version__
    from remembra.relay.adapters import REGISTRY

    plans = plans_module()

    return {
        "source": f"package (remembra {__version__})",
        "plans": plan_facts(),
        "founding": {"annual_price_cents": plans.FOUNDING_ANNUAL_PRICE_CENTS, "max_redemptions": plans.FOUNDING_MAX_REDEMPTIONS},
        "adapters": [
            {"agent": name, "display": words.agent_name(name), "verified": a.spec.verified, "notes": a.spec.notes}
            for name, a in REGISTRY.items()
        ],
        "commands": {
            "install": cmd.PIPX_INSTALL,
            "save_key": cmd.INSTALL_KEEP_SERVER,
            "connect": cmd.connect(),
            "doctor": cmd.doctor(),
            "uninstall": [c for c, _ in cmd.UNINSTALL_STEPS],
        },
        # true for any server this package runs: the code ships, the server flag decides (default off)
        "crew": (
            f"Crew mode ships in remembra {__version__} and is off by default: a server turns it on with "
            "REMEMBRA_CREW_MODE=true. Guide: https://docs.remembra.dev/relay/crew/"
        ),
        "windows": "Windows setup is not tested.",
        "pricing_page": READ_PAGES["pricing"],
    }
