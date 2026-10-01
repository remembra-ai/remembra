"""Every model answer is checked before anyone sees it; anything doubtful becomes the rules' own answer.

:func:`validate_answer` applies the rules of plan 4.3 in order; the first that
fails is the ``fallback_reason``. The answer must cite this turn's successful
reads, carry only template commands, quote only text a read holds, take every
figure from a read, and speak in Marshal's voice (no first person, no
exclamation, no emoji, at most three short sentences). A failed answer is
replaced by :func:`fallback_answer`: ``That's all I can confirm.`` with the
reads' deterministic summaries, the rules verdict's own commands and the
contact page. Pure: no I/O.

Quotes and figures are sourced from the reads the answer cites (its
``evidence``) and the prompt's own facts: every number and every string of
those reads. Model arguments never establish evidence. Only bundled docs, rule-generated
verdict prose and fixed facts authorize quoted claims. Raw timestamps would
source any small number (a day, an hour); their ``_ago`` siblings ("2h ago",
"Sep 17") are what an answer's times come from. A figure keeps its unit: a
``$12`` is sourced only by a ``$12``, a ``12%`` only by a ``12%``.

A pricing answer (a price word, a ``$``, or a number a month) must quote a
cited ``docs_lookup`` section, in double quotes, with no ``$`` outside them
(spec 5: "Pricing ... answers are always quotes plus a link"); it then
carries that section's page as ``doc``, which the dashboard shows with "From
remembra.dev pages; the page governs."
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from remembra.marshal import commands
from remembra.marshal.desk.constants import ALLOWED_LINK_HOSTS, CONTACT_URL, FALLBACK_TEXT
from remembra.marshal.desk.events import AnswerEvent, CommandLine, EvidenceRef
from remembra.marshal.desk.tools import ReadResult
from remembra.marshal.diagnosis import canonical_agent_id
from remembra.relay import outbox
from remembra.relay.adapters import REGISTRY
from remembra.security.secrets import redact_secrets
from remembra.security.untrusted import _BARE_HOST_PATH_RE, detect_actionable

MAX_TEXT_CHARS = 600
MAX_EVIDENCE = 4
MAX_COMMANDS = 2
MAX_SENTENCES = 3
MAX_SENTENCE_WORDS = 19
EVIDENCE_LABEL_CHARS = 72

_CODE_RE = re.compile(r"`([^`\n]*)`")
_QUOTE_RE = re.compile(r"\"([^\"\n]*)\"|“([^”\n]*)”")
_ID_LIKE_RE = re.compile(r"^[A-Za-z0-9._/~:@-]{1,80}$")
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`)\]]+", re.IGNORECASE)
_BANNED_RE = re.compile(
    r"\b(?:SOC ?2|HIPAA|ISO ?27001|PCI|GDPR[- ]compliant|end-to-end encrypted|guarantee(?:s|d)?|SLA|99\.9"
    r"|refund(?:s|ed|able)?)\b",
    re.IGNORECASE,
)
# Chat-assistant filler (spec 4.6), as patterns: this source never spells them out (the voice lint holds that).
VOICE_BANNED_RE = re.compile(
    r"How\s+can\s+I\s+help|I'm\s+here\s+to\s+help|Great\s+question|Sure\!|AI\s+Assistant|I'm\s+sorry|Sorry\s*,"
    r"|^\s*(?:Hi|Hello|Sorry)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(r"\b(?:I|I'm|I've|I'll|I'd)\b")
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-⛿✀-➿]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?])\s+")
FIGURE_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?(?!\d)")
_KEY_RE = re.compile(r"rem_[A-Za-z0-9]")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_ACTIONABLE_ALLOWED = ALLOWED_LINK_HOSTS  # host prefixes, as detect_actionable compares them
_NUMBER_WORD = (
    r"(?:\d[\d,.]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty"
    r"|hundred|thousand)"
)
# Text that talks money: a dollar sign, a price word, a discount, or a number a month / year / seat.
_PRICE_RE = re.compile(
    r"\$|\b(?:prices?|priced|pricing|costs?|costing|dollars?|usd|discount\w*|promo\w*|coupons?|per\s+seat)\b"
    rf"|%\s*off\b|\b{_NUMBER_WORD}\s*(?:a|an|per|/)\s*(?:month|year|seat)\b|/\s*(?:month|year|mo|yr)\b",
    re.IGNORECASE,
)
PRICING_DOC = "https://remembra.dev/pricing"

# What an answer may name as a file (the agents' hook files and the relay's own), shown as ``~/...``.
_HOME = Path("~")
KNOWN_PATHS: frozenset[str] = frozenset(
    {str(adapter.spec.config_path(_HOME)) for adapter in REGISTRY.values()}
    | {str(_HOME / ".codex" / "config.toml"), str(_HOME / ".remembra"), str(_HOME / ".remembra" / "credentials")}
    | {str(outbox.relay_dir(_HOME)), str(outbox.outbox_dir(_HOME)), str(outbox.log_path(_HOME))}
)
# Programs that run something. One followed by an argument must start a desk template (rule 8b).
_EXECUTABLES = frozenset(
    re.findall(
        r"\S+",
        "npm npx pnpm yarn pip pip3 pipx uv uvx brew apt apt-get dnf yum sudo rm mv cp chmod chown curl wget bash sh zsh"
        " python python3 node deno bun cargo gem iex powershell remembra-relay remembra-install",
    )
)
_GIT_RISKY = frozenset(re.findall(r"\S+", "reset checkout clean restore stash push rebase rm config"))
# Remembra's own programs are also nouns in prose ("the remembra-relay hooks"): only a subcommand or a flag after one
# makes a command. ``tests/test_marshal_desk_validate.py`` holds this to the relay CLI's own subcommands.
RELAY_SUBCOMMANDS = frozenset(re.findall(r"\S+", "brief close connect disconnect doctor projects resolve status trail"))
_OWN_PROGRAMS = frozenset({"remembra-relay", "remembra-install"})
# Imperatives whose object, when it reads as a command or a path, must be a desk template.
_IMPERATIVES = frozenset(
    re.findall(
        r"\S+", "run execute exec install reinstall paste enter type re-enter reenter download launch source delete remove"
    )
)
# Words that end an argument list in prose ("... there", "... then restart").
_STOPWORDS = frozenset(
    re.findall(
        r"\S+",
        "then there here in on and to from with at for now first again after before is isn't are was has hasn't can"
        " can't does doesn't not if when so but or that this it its",
    )
)
_WRAP = '"“”‘’()[]<>`*'
_CLAUSE_END = ",;:.!?"
_PATH_START = ("./", "../", "~/", "/")
_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", ">", ">>", "<", "&"})
_COMMANDISH_RE = re.compile(r"[-_./~]")
_SCRIPT_RE = re.compile(r"\.(?:sh|bash|zsh|py|js|mjs|ts|rb|pl|ps1|bat|cmd|exe|command)$", re.IGNORECASE)
# A bare word ending in one of these, with no path, is a file name, not a host.
_FILE_EXTENSIONS = frozenset(
    ["json", "toml", "yaml", "yml", "md", "txt", "log", "lock", "cfg", "ini", "env", "html", "css", "tsx", "jsx", "ts"]
)


@dataclass(frozen=True)
class Answer:
    text: str
    evidence: list[EvidenceRef]
    commands: list[CommandLine]
    fallback: bool = False
    fallback_reason: str | None = None
    summary: list[str] = field(default_factory=list)
    doc: str | None = None

    def as_event(self) -> AnswerEvent:
        return {
            "text": self.text,
            "evidence": list(self.evidence),
            "commands": list(self.commands),
            "fallback": self.fallback,
            "fallback_reason": self.fallback_reason,
            "summary": list(self.summary),
            "doc": self.doc,
        }


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Sources: what an answer may quote and which figures it may use
# ---------------------------------------------------------------------------


def canonical_figure(figure: str) -> str:
    """``$1,200.50`` -> ``$1200.5``; ``12.50%`` -> ``12.5%``; ``0.0020`` -> ``0.002``; ``09`` -> ``9``.

    The unit stays, so a count never sources a price or a percentage.
    """
    prefix = "$" if figure.startswith("$") else ""
    suffix = "%" if figure.endswith("%") else ""
    value = figure.removeprefix("$").removesuffix("%").replace(",", "")
    whole, dot, fraction = value.partition(".")
    whole = str(int(whole)) if whole.isdigit() else whole
    fraction = fraction.rstrip("0") if dot else ""
    return f"{prefix}{whole}.{fraction}{suffix}" if fraction else f"{prefix}{whole}{suffix}"


def figures_in(text: str) -> list[str]:
    return [canonical_figure(m.group(0)) for m in FIGURE_RE.finditer(text)]


def _number_forms(value: int | float) -> set[str]:
    if isinstance(value, int):
        return {str(value)}
    forms = {canonical_figure(f"{round(value, places):.{places}f}") for places in (2, 3, 4, 6) if value >= 0}
    if value.is_integer():
        forms.add(str(int(value)))
    return forms


_ID_KEYS = frozenset({"id", "agent_id", "project_id", "anchor", "projects", "agents"})


@dataclass
class Sources:
    strings: list[str] = field(default_factory=list)
    claim_strings: list[str] = field(default_factory=list)
    figures: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)  # whole values of id fields (an answer may put these in `code`)

    def add(self, value: Any, key: str | None = None, *, trusted_text: bool = False) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int | float):
            self.figures |= _number_forms(value)
        elif isinstance(value, str):
            self.strings.append(value)
            if trusted_text:
                self.claim_strings.append(value)
            if (trusted_text or (key is not None and key.endswith("_ago"))) and not _TIMESTAMP_RE.match(value):
                # A raw timestamp would source every small number (its day, hour and minutes); its
                # "<field>_ago" sibling is the form a figure may come from ("2h ago", "Sep 17").
                self.figures.update(figures_in(value))
            if key in ("project_id", "projects"):
                self.projects.add(value)
            if key in _ID_KEYS:
                self.ids.add(value)
        elif isinstance(value, Mapping):
            for k, item in value.items():
                self.add(item, str(k), trusted_text=trusted_text)
        elif isinstance(value, list | tuple):
            for item in value:
                self.add(item, key, trusted_text=trusted_text)


def gather_sources(reads: Sequence[ReadResult], facts: Iterable[str] = ()) -> Sources:
    sources = Sources()
    for read in reads:
        payload = read.payload
        if read.tool == "brief_preview" and not isinstance(payload.get("handoff"), Mapping):
            payload = {k: v for k, v in payload.items() if k != "project_id"}
        sources.add(payload, trusted_text=read.tool == "docs_lookup")
        if read.tool == "diagnose_agent":
            verdict = read.payload.get("verdict") or {}
            # Verdict prose is generated by rules. Evidence labels and key names can be user supplied.
            sources.add({k: verdict.get(k) for k in ("verdict", "detail", "causes", "then", "caveat")}, trusted_text=True)
    for fact in facts:
        sources.add(fact, trusted_text=True)
    return sources


def _normalise(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The answer and its fallback
# ---------------------------------------------------------------------------


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def evidence_ref(read: ReadResult) -> EvidenceRef:
    return {"ref": read.id, "label": _cut(f"{read.label} · {read.summary}", EVIDENCE_LABEL_CHARS), "anchor": read.anchor}


def command_line(text: str, kind: commands.DeskCommandKind) -> CommandLine:
    return {"text": text, "kind": kind, "prompt": "$" if kind == "terminal" else ">"}


def fallback_answer(reason: str, reads: Sequence[ReadResult], *, server_urls: Iterable[str | None] = ()) -> Answer:
    """``That's all I can confirm.`` with what the rules read, the rules verdict's commands and a person to ask."""
    ok = [read for read in reads if read.ok]
    urls = list(server_urls)
    summary: list[str] = []
    lines: list[CommandLine] = []
    for read in ok:
        summary.append(f"{read.label} · {read.summary}")
        if read.tool != "diagnose_agent":
            continue
        verdict = read.payload.get("verdict") or {}
        summary.append(f"{read.payload.get('agent_id')}: {verdict.get('verdict')} {'[!!]' if verdict.get('proven') else '[??]'}")
        for text in verdict.get("commands") or []:
            kind = commands.desk_command_kind(text, server_urls=urls, projects=())
            if kind and len(lines) < MAX_COMMANDS and all(line["text"] != text for line in lines):
                lines.append(command_line(text, kind))
    return Answer(
        text=FALLBACK_TEXT,
        evidence=[evidence_ref(read) for read in ok],
        commands=lines,
        fallback=True,
        fallback_reason=reason,
        summary=summary,
        doc=CONTACT_URL,
    )


def _parse(raw: str) -> tuple[str, list[str], list[str]]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise _Refused("unparseable") from None
    if not isinstance(data, dict) or set(data) != {"text", "evidence", "commands"}:
        raise _Refused("unparseable")
    text, evidence, cmds = data["text"], data["evidence"], data["commands"]
    if not isinstance(text, str):
        raise _Refused("unparseable")
    for items in (evidence, cmds):
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise _Refused("unparseable")
    return text, evidence, cmds


def _outside(text: str, pattern: re.Pattern[str], placeholder: str = " ") -> str:
    return pattern.sub(placeholder, text)


def _is_agent(word: str) -> bool:
    return canonical_agent_id(word.lower()) in REGISTRY


def _span_allowed(span: str, server_urls: list[str | None], turn: Sources) -> bool:
    """A code span may be a desk command, an agent id, a whole id a read gave, a known file, or a plain token."""
    if commands.desk_command_kind(span, server_urls=server_urls, projects=turn.projects) is not None:
        return True
    if _is_agent(span) or span in turn.ids or span in KNOWN_PATHS:
        return True
    # A flag, a code or a name, but never a path or a script: those only come from the known files above.
    return bool(_ID_LIKE_RE.match(span)) and "/" not in span and not span.startswith((".", "~")) and not _SCRIPT_RE.search(span)


def _words(text: str) -> list[tuple[str, bool]]:
    """``(word, ends_a_clause)`` for each word of ``text``, quote marks, brackets and backticks stripped."""
    words: list[tuple[str, bool]] = []
    for raw in text.split():
        word = raw.strip(_WRAP)
        end = False
        while word and word[-1] in _CLAUSE_END:
            end = True
            word = word[:-1].rstrip(_WRAP)
        if word:
            words.append((word, end))
        elif end and words:
            words[-1] = (words[-1][0], True)
    return words


def _starts_a_command(
    words: list[tuple[str, bool]], start: int, lead: str | None, server_urls: list[str | None], turn: Sources, accepted: set[str]
) -> bool:
    """The words from ``start``, alone or after ``lead``, begin with a desk command that the prose then leaves.

    The longest prefix (to the clause's end) that is a desk command counts; a flag or a shell operator right
    after it means a longer command than the template, so it doesn't.
    """
    run: list[str] = []
    longest = 0
    for n, (word, end) in enumerate(words[start : start + 12], start=1):
        run.append(word)
        phrase = " ".join(run)
        for candidate in (phrase, f"{lead} {phrase}" if lead else None):
            if candidate is None:
                continue
            if candidate in accepted or commands.desk_command_kind(candidate, server_urls=server_urls, projects=turn.projects):
                longest = n
        if end:
            break
    if not longest:
        return False
    after = start + longest
    if words[after - 1][1] or after >= len(words):
        return True
    return not (words[after][0].startswith("-") or words[after][0] in _SHELL_OPERATORS)


def _command_in_prose(text: str, server_urls: list[str | None], turn: Sources, accepted: set[str]) -> bool:
    """True when ``text`` (quotes and code spans included) tells the user to run something that is no desk template.

    A program word (``npm``, ``pip``, ``brew``, ``curl``, ``rm``, a risky ``git`` ...) followed by an argument, or
    an imperative (``run``, ``install``, ``paste`` ...) whose object reads as a command or a path, must start one
    of the desk's commands; a path in an imperative's next three words must be one of them or a known file.
    """
    words = _words(text)
    for i, (word, end) in enumerate(words):
        low = word.lower()
        following = words[i + 1][0] if i + 1 < len(words) and not end else None
        if following is not None and following.lower() not in _STOPWORDS:
            if low in _OWN_PROGRAMS:
                risky = following.startswith("-") or following in RELAY_SUBCOMMANDS
            else:
                risky = low in _EXECUTABLES or (low == "git" and following.lower() in _GIT_RISKY)
            if risky and not _starts_a_command(words, i, None, server_urls, turn, accepted):
                return True
        if low not in _IMPERATIVES or following is None:
            continue
        if not (_is_agent(following) or following in turn.ids or following in KNOWN_PATHS):
            commandish = following.startswith(_PATH_START) or following.lower() in _EXECUTABLES
            if (commandish or _COMMANDISH_RE.search(following)) and not _starts_a_command(
                words, i + 1, low, server_urls, turn, accepted
            ):
                return True
        for j in range(i + 1, min(i + 4, len(words))):
            path, path_end = words[j]
            if path.startswith(_PATH_START) and path not in KNOWN_PATHS:
                if not _starts_a_command(words, j, low, server_urls, turn, accepted):
                    return True
            if path_end:
                break
    return False


def _foreign_host(text: str, allowed_hosts: set[str]) -> bool:
    """True when ``text`` names a host (no scheme needed: ``remembra-support.io/renew``) that isn't Remembra's."""
    for raw in text.split():
        word = raw.strip(_WRAP + "'")
        while word and word[-1] in _CLAUSE_END:
            word = word[:-1].rstrip(_WRAP + "'")
        if not word or "://" in word or not _BARE_HOST_PATH_RE.match(word):
            continue
        host = word.lstrip("/").split("/")[0].split("?")[0].split("#")[0].split(":")[0].lower()
        if "/" not in word.lstrip("/") and host.rsplit(".", 1)[-1] in _FILE_EXTENSIONS:
            continue  # hooks.json, config.toml: a file name
        if host not in allowed_hosts:
            return True
    return False


def _docs_page(quotes: list[str], cited: Sequence[ReadResult]) -> str | None:
    """The page of the first cited ``docs_lookup`` section that holds one of ``quotes``, or None."""
    for read in cited:
        if read.tool != "docs_lookup":
            continue
        for section in read.payload.get("sections") or []:
            body = _normalise(str(section.get("text") or ""))
            if any(quote and quote in body for quote in quotes):
                url = section.get("url")
                return url if isinstance(url, str) and url else PRICING_DOC
    return None


def _check(raw: str, reads: Sequence[ReadResult], server_urls: list[str | None], turn: Sources, facts: Sequence[str]) -> Answer:
    text, evidence, cmds = _parse(raw)
    text = text.strip()
    # 2. length
    if not 1 <= len(text) <= MAX_TEXT_CHARS:
        raise _Refused("too_long")
    # 3-5. evidence: this turn's successful reads
    if not 1 <= len(evidence) <= MAX_EVIDENCE or len(set(evidence)) != len(evidence):
        raise _Refused("no_evidence")
    by_id = {read.id: read for read in reads}
    if any(ref not in by_id for ref in evidence):
        raise _Refused("unknown_evidence")
    if any(not by_id[ref].ok for ref in evidence):
        raise _Refused("failed_read_evidence")
    cited = [by_id[ref] for ref in evidence]
    sources = gather_sources(cited, facts)
    # 6. commands: templates only
    if len(cmds) > MAX_COMMANDS:
        raise _Refused("command_not_allowed")
    lines: list[CommandLine] = []
    for cmd in cmds:
        kind = commands.desk_command_kind(cmd, server_urls=server_urls, projects=turn.projects)
        if kind is None:
            raise _Refused("command_not_allowed")
        targets = re.findall(r"(?:--agent |for )([A-Za-z0-9-]+)", cmd)
        for read in reads:
            verdict = read.payload.get("verdict") or {}
            if (
                read.tool == "diagnose_agent"
                and read.payload.get("agent_id") in targets
                and verdict.get("proven") is True
                and cmd not in (verdict.get("commands") or [])
            ):
                raise _Refused("command_contradicts_verdict")
        lines.append(command_line(cmd, kind))
    # 7. code spans: an accepted command, an agent id, a whole id from a read, a known file, or a flag / code
    for span in _CODE_RE.findall(text):
        if not _span_allowed(span, server_urls, turn):
            raise _Refused("command_in_text")
    # 8. nothing that reads as something to run (links are rule 9's)
    if detect_actionable(_URL_RE.sub("link", text), allowed_url_prefixes=_ACTIONABLE_ALLOWED):
        raise _Refused("command_in_text")
    # 8b. a program with arguments, or a run / install of something, is a desk template (quotes included)
    if _command_in_prose(_URL_RE.sub("link", text), server_urls, turn, set(cmds)):
        raise _Refused("command_in_text")
    # 9. links to Remembra's own pages only
    for url in _URL_RE.findall(text):
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            raise _Refused("link_not_allowed") from None
        if host not in ALLOWED_LINK_HOSTS or "@" in parts.netloc or parts.port is not None:
            raise _Refused("link_not_allowed")
    # 9b. no host named without a scheme either, but Remembra's and this server's
    server_hosts = {(urlsplit(url).hostname or "").lower() for url in server_urls if url}
    if _foreign_host(_URL_RE.sub("link", text), {*ALLOWED_LINK_HOSTS, *server_hosts}):
        raise _Refused("link_not_allowed")
    # 10. nothing key-shaped, in the text or a command
    for value in (text, *cmds):
        if redact_secrets(value).counts or _KEY_RE.search(value):
            raise _Refused("key_shaped")
    # 11. every quote is text a read (or the prompt's own facts) holds
    haystack = [_normalise(s) for s in sources.claim_strings]
    quotes = [_normalise(m.group(1) if m.group(1) is not None else m.group(2)) for m in _QUOTE_RE.finditer(text)]
    for quoted in quotes:
        if quoted and not any(quoted in source for source in haystack):
            raise _Refused("unverified_quote")
    unquoted = _outside(text, _QUOTE_RE)
    # 12. no compliance, uptime or refund claims outside a verified quote
    if _BANNED_RE.search(unquoted):
        raise _Refused("banned_phrase")
    # 13. no chat-assistant filler anywhere
    if VOICE_BANNED_RE.search(text):
        raise _Refused("voice_banned")
    # 14. no first person
    if _FIRST_PERSON_RE.search(unquoted):
        raise _Refused("first_person")
    # 15. no exclamation mark outside quotes and code
    plain = _outside(unquoted, _CODE_RE)
    if "!" in plain:
        raise _Refused("exclamation")
    # 16. no emoji
    if _EMOJI_RE.search(text):
        raise _Refused("emoji")
    # 17. at most 3 sentences, each at most 19 words
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(_outside(text, _CODE_RE, "code").strip()) if s.strip()]
    if len(sentences) > MAX_SENTENCES:
        raise _Refused("too_many_sentences")
    if any(len(sentence.split()) > MAX_SENTENCE_WORDS for sentence in sentences):
        raise _Refused("sentence_too_long")
    # 18. every figure outside code and quotes comes from a cited read, in its unit
    if any(figure not in sources.figures for figure in figures_in(plain)):
        raise _Refused("unsourced_figure")
    # 19. a price is a quote of a cited docs section, never a figure of the model's own; it carries its page
    doc: str | None = None
    if _PRICE_RE.search(plain) or any("$" in quoted for quoted in quotes):
        doc = _docs_page(quotes, cited)
        stray = [q for q in quotes if "$" in q and _docs_page([q], cited) is None]
        if "$" in plain or doc is None or stray:
            raise _Refused("pricing_unquoted")
    return Answer(text=text, evidence=[evidence_ref(read) for read in cited], commands=lines, doc=doc)


def validate_answer(
    raw: str | None, reads: Sequence[ReadResult], *, server_urls: Iterable[str | None], facts: Iterable[str] = ()
) -> Answer:
    """The model's answer if it passes every rule, else :func:`fallback_answer` with the first failed rule."""
    urls = list(server_urls)
    fact_list = list(facts)
    turn = gather_sources(reads, fact_list)
    try:
        return _check(raw or "", reads, urls, turn, fact_list)
    except _Refused as refused:
        return fallback_answer(refused.reason, reads, server_urls=urls)
