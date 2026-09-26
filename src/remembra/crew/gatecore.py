"""Crew gatecore (WP-3): the stdlib-only decision core shared by the hook gate and the server guard.

Spec: §5.2 (guard table), §8.2 (target extraction, Bash parser, MCP tool map,
tamper commands), §5.3 (agent-facing text), §10.3 (offline decisions), §11
(injection hygiene) and D11, D12, D26, D28, D31, D33, D34, D38.

What lives here:

* **Bash parser** (:func:`parse_bash`): a quote-aware shell lexer plus a
  per-command classifier that finds write targets, tree writers, tree-wide git
  operations, tamper forms and opaque segments. It never executes anything and
  never runs a user-supplied regex. Contract: ``docs/crew/bash-parser.md`` and
  ``tests/crew/vectors/bash/corpus.json``.
* **Paths and globs**: NFC normalisation, case folding on case-insensitive
  checkouts, checkout mapping (own / foreign / none), a segment glob matcher
  with ``**`` and no regex (:func:`glob_match`), and directory overlap.
* **Zones** (:class:`ZoneIndex`): zone resolution with nesting, commons,
  ignore, the built-in ``crew-policy`` zone, and the command-pattern token trie
  (:class:`CommandTrie`, D38).
* **Predicates and decision** (:func:`evaluate`): derives the §5.2 predicates
  for every target of one tool call from the local snapshot, runs the
  first-match table (:func:`remembra.crew.schemas.guard_decide`), performs the
  auto-claim step through a caller-supplied callback (D11) and renders the
  deny reason.
* **Tamper detection**: tamper commands (row 2) and surgical protection of
  crew hook entries in Claude settings, Husky and lefthook files (D28).
* **MCP tool map**: the built-in map from :mod:`remembra.crew.schemas` plus
  zones.yml ``mcp_tools``, mapped to path, zone and service targets.
* **Digest and templates**: the UserPromptSubmit digest, the YOU and DO NOT
  TOUCH lines, and every deny, Stop and turn template (ids, slugs and
  callsigns only; free text only inside the ``<remembra-data>`` block).

Stdlib only. The vendored gate (``crew-gate.py``, ``python -I``) imports this
module and :mod:`remembra.crew.schemas`; a test runs it with every non-stdlib
import blocked, and another runs a plain copy of the two files as a package
under ``python -I -S``.

How callers use it:

* **The hook gate (WP-9)** calls :func:`evaluate_hook_payload` with the
  PreToolUse payload, the local snapshot, the session id resolved through crewd,
  the server-adjusted ``now`` and a ``claim`` callback that asks crewd for an
  auto-claim (≤900 ms, D11). It prints :meth:`Verdict.hook_stdout`, spools the
  ``effects`` (``guard.blocked``, ``claim.unconfirmed``, ``gate.deadline``,
  ``would_deny``, collisions) and runs the post-tool check when
  ``post_tool_check`` is set. Read-only Bash and read-like MCP tools return
  before the snapshot is touched.
* **The server guard (WP-5)** uses the same :func:`evaluate` with a snapshot
  whose ``checkouts`` holds one synthetic checkout for the caller (so
  repo-relative paths map to it) and ``claim=None`` or its own claim function.
* **Offline (D12)**: pass ``server_reachable=False``; same-host holders and
  ``fail_closed`` zones still deny, remote holders count only while the
  snapshot is under 30 min old and the skew is within 5 min.
  ``server_outage=True`` keeps the holder's own claims writable past the lease
  horizon (§8.1).
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat as _stat
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Final, Protocol

from remembra.crew import schemas as S

GATECORE_VERSION: Final = 1

# ===========================================================================
# Bash lexer
# ===========================================================================

_OPS_2: Final = ("&&", "||", ";;", "|&")
_SEP_OPS: Final = frozenset({"&&", "||", ";", "|", "&", "\n", "|&", ";;"})
_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\+?=)(.*)", re.DOTALL)
_BYPASS_CODE_RE: Final = re.compile(S.BYPASS_CODE_PATTERN)


@dataclass
class Word:
    """One shell word after quote removal, with flags about what the shell would expand."""

    value: str = ""
    raw: str = ""
    quoted: bool = False
    has_var: bool = False  # $X, ${X}, $1 … (unquoted or inside double quotes)
    has_subst: bool = False  # $( … ), backticks, $(( … )), <( … )
    has_glob: bool = False  # unquoted * ? [
    has_brace: bool = False  # unquoted {a,b} / {1..3}
    quote_start: int | None = None  # index in ``value`` where the first quoted or escaped text begins

    @property
    def name_unquoted(self) -> bool:
        """An assignment prefix ``NAME=`` counts only when the name and ``=`` are unquoted."""
        eq = self.value.find("=")
        return eq > 0 and (self.quote_start is None or self.quote_start > eq)

    @property
    def dynamic(self) -> bool:
        """The shell would change this word before the command sees it (variable, glob, braces, substitution)."""
        return self.has_var or self.has_subst or self.has_glob or self.has_brace


@dataclass
class Tok:
    kind: str  # word | op | redir
    word: Word | None = None
    op: str = ""
    fd: str | None = None
    target: Word | None = None  # redirection target (or heredoc delimiter)
    heredoc_body: str | None = None
    start: int = 0
    end: int = 0


class _LexError(Exception):
    pass


def _is_word(tok: Tok, value: str) -> bool:
    return tok.kind == "word" and tok.word is not None and tok.word.value == value


def _scan_balanced(s: str, i: int, open_ch: str, close_ch: str) -> int:
    """``s[i]`` is just after an opening ``open_ch``; return the index just past the matching ``close_ch``."""
    depth = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'" and open_ch == "(":
            j = s.find("'", i + 1)
            if j < 0:
                raise _LexError("unterminated quote")
            i = j + 1
            continue
        if c == '"':
            i = _scan_dquote(s, i + 1, _WB())
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise _LexError("unterminated backtick")
            i = j + 1
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _LexError(f"unbalanced {open_ch}")


class _WB:
    """Word builder: collects text in lists and joins once (linear time on long words, §10.3 deadline)."""

    __slots__ = ("val", "raw", "vlen", "quoted", "has_var", "has_subst", "has_glob", "has_brace", "quote_start")

    def __init__(self) -> None:
        self.val: list[str] = []
        self.raw: list[str] = []
        self.vlen = 0
        self.quoted = False
        self.has_var = False
        self.has_subst = False
        self.has_glob = False
        self.has_brace = False
        self.quote_start: int | None = None

    def add(self, value: str, raw: str | None = None) -> None:
        if value:
            self.val.append(value)
            self.vlen += len(value)
        self.raw.append(value if raw is None else raw)

    def add_raw(self, raw: str) -> None:
        self.raw.append(raw)

    def mark_quote(self) -> None:
        self.quoted = True
        if self.quote_start is None:
            self.quote_start = self.vlen

    def word(self) -> Word:
        return Word(
            value="".join(self.val),
            raw="".join(self.raw),
            quoted=self.quoted,
            has_var=self.has_var,
            has_subst=self.has_subst,
            has_glob=self.has_glob,
            has_brace=self.has_brace,
            quote_start=self.quote_start,
        )


def _scan_dollar(s: str, i: int, w: _WB) -> int:
    """``s[i] == '$'``: consume one expansion into ``w`` and return the next index."""
    n = len(s)
    nxt = s[i + 1] if i + 1 < n else ""
    if nxt == "(":
        end = _scan_balanced(s, i + 2, "(", ")")
        w.has_subst = True
        w.add(s[i:end])
        return end
    if nxt == "{":
        end = _scan_balanced(s, i + 2, "{", "}")
        w.has_var = True
        w.add(s[i:end])
        return end
    if nxt and (nxt.isalnum() or nxt in "_@*#?$!-"):
        j = i + 1
        if nxt.isalpha() or nxt == "_":
            m = _NAME_RE.match(s, j)
            j = m.end() if m else j + 1
        else:
            j += 1
        w.has_var = True
        w.add(s[i:j])
        return j
    w.add("$")
    return i + 1


_DQ_PLAIN_RE: Final = re.compile(r'[^"\\$`]+')


def _scan_dquote(s: str, i: int, w: _WB) -> int:
    """Inside double quotes starting at ``i`` (just after the quote); return index after the closing quote."""
    n = len(s)
    while i < n:
        m = _DQ_PLAIN_RE.match(s, i)
        if m is not None:  # a run of plain characters in one slice
            w.add(m.group())
            i = m.end()
            continue
        c = s[i]
        if c == '"':
            w.add_raw('"')
            return i + 1
        if c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
            w.add(s[i + 1] if s[i + 1] != "\n" else "", s[i : i + 2])
            i += 2
            continue
        if c == "$":
            i = _scan_dollar(s, i, w)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise _LexError("unterminated backtick")
            w.has_subst = True
            w.add(s[i : j + 1])
            i = j + 1
            continue
        w.add(c)
        i += 1
    raise _LexError("unterminated double quote")


_WORD_BREAK: Final = frozenset(" \t\n;&|()<>")


def _lex(s: str) -> list[Tok]:
    """Tokenise a shell command line. Raises :class:`_LexError` on unbalanced quoting."""
    toks: list[Tok] = []
    pending_heredocs: list[Tok] = []
    i = 0
    n = len(s)
    at_word_start = True
    while i < n:
        c = s[i]
        if c in " \t":
            i += 1
            continue
        if c == "\\" and i + 1 < n and s[i + 1] == "\n":
            i += 2
            continue
        if c == "\n":
            toks.append(Tok("op", op="\n", start=i, end=i + 1))
            i += 1
            if pending_heredocs:
                i = _read_heredocs(s, i, pending_heredocs)
                pending_heredocs = []
            continue
        if c == "#" and at_word_start:
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        # operators
        two = s[i : i + 2]
        if two in _OPS_2:
            toks.append(Tok("op", op=two, start=i, end=i + 2))
            i += 2
            continue
        if c in ";&|":
            if c == "&" and s[i + 1 : i + 2] == ">":
                op = "&>>" if s[i + 2 : i + 3] == ">" else "&>"
                i = _lex_redir_target(s, i + len(op), toks, op, None, i)
                continue
            toks.append(Tok("op", op=c, start=i, end=i + 1))
            i += 1
            continue
        if c in "()":
            if c == "(" and toks and toks[-1].kind == "redir" and toks[-1].target is None:
                pass
            toks.append(Tok("op", op=c, start=i, end=i + 1))
            i += 1
            continue
        if c in "<>":
            if s[i + 1 : i + 2] == "(":  # process substitution <( … ) / >( … )
                end = _scan_balanced(s, i + 2, "(", ")")
                w = Word(value=s[i:end], raw=s[i:end], has_subst=True)
                toks.append(Tok("word", word=w, start=i, end=end))
                i = end
                continue
            op, j = _redir_op(s, i)
            if op in ("<<", "<<-"):
                i = _lex_heredoc_marker(s, j, toks, op, None, i, pending_heredocs)
            else:
                i = _lex_redir_target(s, j, toks, op, None, i)
            continue
        # a word (possibly an fd-number prefix of a redirection)
        w, j = _lex_word(s, i)
        if j < n and s[j] in "<>" and w.raw.isdigit() and s[j + 1 : j + 2] != "(":
            op, k = _redir_op(s, j)
            if op in ("<<", "<<-"):
                i = _lex_heredoc_marker(s, k, toks, op, w.raw, i, pending_heredocs)
            else:
                i = _lex_redir_target(s, k, toks, op, w.raw, i)
            continue
        toks.append(Tok("word", word=w, start=i, end=j))
        i = j
    if pending_heredocs:
        # heredoc with no body line (command at the end of input): treat the body as empty
        for t in pending_heredocs:
            t.heredoc_body = ""
    return toks


def _redir_op(s: str, i: int) -> tuple[str, int]:
    for op in ("<<<", "<<-", ">>", ">|", ">&", "<&", "<>", "<<", ">", "<"):
        if s.startswith(op, i):
            return op, i + len(op)
    raise _LexError("bad redirection")  # pragma: no cover - callers only call on < or >


def _skip_blanks(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _lex_redir_target(s: str, i: int, toks: list[Tok], op: str, fd: str | None, start: int) -> int:
    i = _skip_blanks(s, i)
    if i >= len(s) or s[i] in "\n;&|()<>":
        raise _LexError("redirection without a target")
    w, j = _lex_word(s, i)
    toks.append(Tok("redir", op=op, fd=fd, target=w, start=start, end=j))
    return j


def _lex_heredoc_marker(s: str, i: int, toks: list[Tok], op: str, fd: str | None, start: int, pending: list[Tok]) -> int:
    i = _skip_blanks(s, i)
    w, j = _lex_word(s, i)
    tok = Tok("redir", op=op, fd=fd, target=w, start=start, end=j)
    toks.append(tok)
    pending.append(tok)
    return j


def _read_heredocs(s: str, i: int, pending: list[Tok]) -> int:
    """Consume heredoc bodies (in order) starting at line index ``i``; return the index after the last delimiter line."""
    for tok in pending:
        assert tok.target is not None
        delim = tok.target.value
        strip_tabs = tok.op == "<<-"
        body: list[str] = []
        while i <= len(s):
            j = s.find("\n", i)
            line = s[i:] if j < 0 else s[i:j]
            nxt = len(s) if j < 0 else j + 1
            check = line.lstrip("\t") if strip_tabs else line
            if check == delim:
                i = nxt
                break
            body.append(line)
            i = nxt
            if j < 0:
                break
        tok.heredoc_body = "\n".join(body)
    return i


def _word_end(s: str, i: int) -> int:
    j = i
    while j < len(s) and s[j] not in _WORD_BREAK:
        j += 1
    return j


# Characters with no special meaning inside an unquoted word (copied a whole run at a time).
_PLAIN_RUN_RE: Final = re.compile(r"[^ \t\n;&|()<>\\'\"$`*?\[{},.]+")


def _lex_word(s: str, i: int) -> tuple[Word, int]:
    """One unquoted-context word starting at ``i``. Linear in the word length (no per-character copies)."""
    w = _WB()
    n = len(s)
    brace_open = False
    brace_sep = False
    word_end = -1  # first word-break index at or after the current position (valid while >= i)
    close_at = -1  # first "]" after the current "[" (valid while > i); n when there is none
    while i < n:
        m = _PLAIN_RUN_RE.match(s, i)
        if m is not None:
            w.add(m.group())
            i = m.end()
            continue
        c = s[i]
        if c in _WORD_BREAK:
            break
        if c == "\\":
            if i + 1 < n:
                if s[i + 1] == "\n":
                    i += 2
                    continue
                w.mark_quote()
                w.add(s[i + 1], s[i : i + 2])
                i += 2
            else:
                i += 1
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _LexError("unterminated single quote")
            w.mark_quote()
            w.add(s[i + 1 : j], s[i : j + 1])
            i = j + 1
            continue
        if c == "$" and s[i + 1 : i + 2] == "'":  # ANSI-C quoting: literal text
            j = i + 2
            buf: list[str] = []
            while j < n and s[j] != "'":
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j : j + 2])
                    j += 2
                    continue
                buf.append(s[j])
                j += 1
            if j >= n:
                raise _LexError("unterminated $' quote")
            w.mark_quote()
            w.add("".join(buf), s[i : j + 1])
            i = j + 1
            continue
        if c == '"':
            w.mark_quote()
            w.add_raw('"')
            i = _scan_dquote(s, i + 1, w)
            continue
        if c == "$":
            i = _scan_dollar(s, i, w)
            continue
        if c == "`":
            j = s.find("`", i + 1)
            if j < 0:
                raise _LexError("unterminated backtick")
            w.has_subst = True
            w.add(s[i : j + 1])
            i = j + 1
            continue
        if c in "*?":
            w.has_glob = True
        elif c == "[":
            if word_end < i:
                word_end = _word_end(s, i)
            if close_at <= i:
                found = s.find("]", i + 1)
                close_at = n if found < 0 else found
            if close_at < word_end:
                w.has_glob = True
        elif c == "{":
            brace_open = True
        elif c == "," and brace_open or c == "." and brace_open and s[i : i + 2] == "..":
            brace_sep = True
        elif c == "}" and brace_open and brace_sep:
            w.has_brace = True
        w.add(c)
        i += 1
    return w.word(), i


# ===========================================================================
# Bash parser
# ===========================================================================

_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "ash"})
_INTERPRETERS: Final = frozenset(
    {"python", "python2", "python3", "node", "nodejs", "perl", "ruby", "php", "deno", "osascript", "lua", "Rscript"}
)
_NEUTRAL_BUILTINS: Final = frozenset(
    {
        "export",
        "unset",
        "set",
        "alias",
        "unalias",
        "cd",
        "pushd",
        "popd",
        "exit",
        "return",
        "shopt",
        "trap",
        "hash",
        "ulimit",
        "read",
        "sleep",
        "wait",
        "declare",
        "typeset",
        "local",
        "readonly",
        "fi",
        "done",
        "esac",
        "}",
    }
)
_EXTRA_READ_ONLY: Final = frozenset({"[", "[[", "test", ":", "basename", "dirname", "realpath", "readlink", "seq", "id"})
_STRIP_KEYWORDS: Final = frozenset({"if", "then", "elif", "else", "do", "while", "until", "!"})
_OPAQUE_KEYWORDS: Final = frozenset({"for", "select", "function", "coproc"})
_REMOVAL_SCAN_RE: Final = re.compile(r"\b(?:rm|rmdir|unlink|rmtree|remove|removedirs|unlinkSync|rmSync|truncate|rimraf)\b")
_KILL_SCAN_RE: Final = re.compile(r"(?<![\w.])(?:kill|pkill|killall|launchctl|systemctl)\b")
_DEV_TARGETS: Final = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"})
_HOOK_ENV_TAMPER: Final[Mapping[str, str]] = {
    "HUSKY": "husky_off",
    "HUSKY_SKIP_HOOKS": "husky_off",
    "LEFTHOOK": "lefthook_off",
    "LEFTHOOK_EXCLUDE": "lefthook_off",
}
# Environment variables that make a command run other code, read other config or write trace files.
# A segment with one of these as a prefix (or exported) is never read-only: it is opaque and raw-scanned.
_EXEC_ENV_EXACT: Final = frozenset(
    {
        "PAGER",
        "MANPAGER",
        "EDITOR",
        "VISUAL",
        "LESS",
        "LESSOPEN",
        "LESSCLOSE",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "IFS",
        "PATH",
        "HOME",
        "NODE_OPTIONS",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PYTHONHOME",
        "PERL5OPT",
        "PERL5LIB",
        "RUBYOPT",
        "RUBYLIB",
        "SSH_ASKPASS",
        "SUDO_ASKPASS",
        "RIPGREP_CONFIG_PATH",
        "GREP_OPTIONS",
        "XDG_CONFIG_HOME",
    }
)
_EXEC_ENV_PREFIXES: Final = ("GIT_", "LD_", "DYLD_")
# Git reads these as extra config (core.hooksPath included): setting them is a hooks-path tamper.
_GIT_CONFIG_ENV_RE: Final = re.compile(r"GIT_CONFIG(?:_COUNT|_KEY_\d+|_VALUE_\d+|_PARAMETERS|_GLOBAL|_SYSTEM)?")
_PM_TEST_SCRIPTS: Final = ("test", "t")
_FORMATTERS: Final = frozenset({"prettier", "eslint", "biome", "ruff", "black", "gofmt"})
_FILE_EXT_RE: Final = re.compile(r"^[^.]*[^/]*\.[A-Za-z0-9_+-]{1,16}$")


@dataclass
class _Seg:
    """One simple command after unwrapping."""

    words: list[Word]
    redirs: list[Tok]
    raw: str
    assigns: list[tuple[str, str, Word]] = field(default_factory=list)


@dataclass
class BashParse:
    """Parse result. :meth:`as_dict` gives the contract keys plus extras used by :func:`evaluate`."""

    read_only: bool = False
    writes: set[str] = field(default_factory=set)
    tree_writer: bool = False
    tree_scope: set[str] = field(default_factory=set)
    tamper: set[str] = field(default_factory=set)
    git_tree_op: str | None = None
    opaque: bool = False
    # extras
    removes: set[str] = field(default_factory=set)  # subset of writes that delete or move away
    dirs: set[str] = field(default_factory=set)  # subset of writes that are (or behave as) directories
    restores: set[str] = field(default_factory=set)  # git checkout/restore from the index or HEAD (no other ref)
    git_tree_op_cwd: str | None = None  # cwd (relative to the start) of the first tree-wide git op
    argvs: list[tuple[str | None, list[str]]] = field(default_factory=list)  # (cwd, normalised argv) per command
    scanned: list[str] = field(default_factory=list)  # raw text of opaque code that was tamper-scanned
    # literal process ids a ``kill`` targets (process groups as their positive id; ``-1`` = every process):
    # :func:`evaluate` makes one naming crewd's pid a ``crewd_kill`` tamper (the gate passes crewd's pid)
    kill_pids: set[int] = field(default_factory=set)
    # targets relative to the checkout's top level (git ``:/`` and ``:(top)`` pathspecs):
    # (cwd of the command, path from the top level, remove, restore, is_dir)
    top_writes: list[tuple[str, str, bool, bool, bool]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "writes": sorted(self.writes),
            "tree_writer": self.tree_writer,
            "tree_scope": sorted(self.tree_scope),
            "tamper": sorted(self.tamper),
            "git_tree_op": self.git_tree_op,
            "opaque": self.opaque,
            "removes": sorted(self.removes),
            "dirs": sorted(self.dirs),
            "restores": sorted(self.restores),
            "git_tree_op_cwd": self.git_tree_op_cwd,
            "argvs": [[cwd, list(argv)] for cwd, argv in self.argvs],
            "top_writes": sorted({rel for _cwd, rel, *_ in self.top_writes}),
        }


def parse_bash(command: str) -> dict[str, Any]:
    """Classify one Bash command line (contract: ``docs/crew/bash-parser.md``).

    Returns ``read_only, writes, tree_writer, tree_scope, tamper, git_tree_op,
    opaque`` plus the extras ``removes, dirs, git_tree_op_cwd, argvs``. Paths
    are relative to the command's starting directory, POSIX-normalised; ``~``
    stays literal. Never raises: unparseable input is ``opaque`` with a raw
    tamper scan.
    """
    return parse_bash_full(command).as_dict()


def parse_bash_full(command: str) -> BashParse:
    res = BashParse()
    try:
        toks = _lex(command)
    except _LexError:
        res.opaque = True
        _raw_scan(command, res)
        return res
    _Parser(command, toks, res).run()
    if res.opaque or res.writes or res.top_writes or res.tree_writer or res.tamper or res.git_tree_op:
        res.read_only = False
    return res


# Claude Code settings keys that switch every hook off. A command that writes or runs code and names
# one is tamper (settings_hook_edit): its effect on a settings file cannot be known before it runs.
_HOOKS_OFF_RE: Final = re.compile(r"disableAllHooks|allowManagedHooksOnly", re.IGNORECASE)


def _raw_scan(text: str, res: BashParse) -> None:
    """Tamper markers in opaque code (``BASH_TAMPER_SCAN``); kill/removal markers need their verb in the same text."""
    res.scanned.append(text)
    if _HOOKS_OFF_RE.search(text):
        res.tamper.add("settings_hook_edit")
    low = text.lower()
    for kind, markers in S.BASH_TAMPER_SCAN.items():
        hit = any((m.lower() in low) if kind == "hooks_path" else (m in text) for m in markers)
        if not hit:
            continue
        if kind == "crewd_kill" and not _KILL_SCAN_RE.search(text):
            continue
        if kind == "crew_files_removed" and not _REMOVAL_SCAN_RE.search(text):
            continue
        if kind == "env_crew_var" and _only_bypass_code_assignments(text):
            continue
        res.tamper.add(kind)


def _only_bypass_code_assignments(text: str) -> bool:
    """``REMEMBRA_BYPASS=<code> git …`` is the human-issued form (D34); anything else setting crew vars is tamper."""
    for m in re.finditer(r"(REMEMBRA_CREW_SESSION|REMEMBRA_CREW|REMEMBRA_BYPASS)=(\S*)", text):
        name, value = m.group(1), m.group(2).strip("'\"")
        if name != "REMEMBRA_BYPASS" or not _BYPASS_CODE_RE.fullmatch(value):
            return False
        before = text[: m.start()].rstrip()
        after = text[m.end() :].lstrip()
        if before.endswith("export") or not after.startswith("git"):
            return False
    return True


def _norm_join(cwd: str | None, path: str) -> str | None:
    """Join ``path`` onto the tracked cwd; ``None`` when the cwd is unknown and the path is relative."""
    if path.startswith("/") or path == "~" or path.startswith("~/"):
        base = path
    elif cwd is None:
        return None
    else:
        base = posixpath.join(cwd, path)
    out = posixpath.normpath(base)
    if out.startswith("//"):
        out = "/" + out.lstrip("/")
    return out


_HOME_VAR_RE: Final = re.compile(r"(?:\$HOME|\$\{HOME\})(?=/|$)")


def _home_word(w: Word) -> Word:
    """``$HOME/x`` or ``${HOME}/x`` with nothing else dynamic is ``~/x`` (the gate resolves ``~`` to the home)."""
    if not w.has_var or w.has_subst or w.has_glob or w.has_brace:
        return w
    m = _HOME_VAR_RE.match(w.value)
    if m is None:
        return w
    rest = w.value[m.end() :]
    if "$" in rest or "`" in rest:
        return w
    return Word(value="~" + rest, raw=w.raw, quoted=w.quoted, quote_start=w.quote_start)


def _exec_env(name: str) -> bool:
    """The variable makes the command run other code or read other config (see ``_EXEC_ENV_EXACT``)."""
    return name in _EXEC_ENV_EXACT or name.startswith(_EXEC_ENV_PREFIXES)


def _long_opt(key: str, options: Iterable[str], *, min_len: int = 3) -> str | None:
    """getopt_long / git parse-options abbreviation: the option ``key`` names exactly, or the only one it prefixes."""
    opts = tuple(options)
    if key in opts:
        return key
    if len(key) < min_len or not key.startswith("--"):
        return None
    hits = [o for o in opts if o.startswith(key)]
    return hits[0] if len(hits) == 1 else None


def _has_ext(path: str) -> bool:
    last = posixpath.basename(path.rstrip("/"))
    if not last or last in (".", ".."):
        return False
    if last.startswith(".") and "." not in last[1:]:
        return True  # dotfiles (.env, .eslintrc) are files
    return bool(_FILE_EXT_RE.match(last))


class _Parser:
    def __init__(self, command: str, toks: list[Tok], res: BashParse) -> None:
        self.cmd = command
        self.toks = toks
        self.res = res
        self.cwd: str | None = "."
        self.dirstack: list[str | None] = []
        self.any_read_only = False
        self.all_read_only = True
        self.seg_raw = ""  # raw text of the segment being classified (raw-scanned when a target is dynamic)
        self.no_ro = False  # the segment cannot be read-only (config or exec-capable env prefix)
        self.exported_exec = False  # an exec-capable variable was exported earlier in this command line
        self.seg_ro = False  # the segment being classified turned out read-only

    # -- driver -------------------------------------------------------------
    def run(self) -> None:
        segs = self._segments()
        for seg in segs:
            if seg is None:
                continue
            self._command(seg)
            if not self.seg_ro and _HOOKS_OFF_RE.search(seg.raw):
                self.res.tamper.add("settings_hook_edit")
        self.res.read_only = self.all_read_only and self.any_read_only

    def _segments(self) -> list[_Seg | None]:
        """Split into simple commands. Subshells, groups and ``case`` are opaque units (raw-scanned)."""
        out: list[_Seg | None] = []
        toks = self.toks
        i = 0
        n = len(toks)
        cur_words: list[Word] = []
        cur_redirs: list[Tok] = []
        cur_start: int | None = None
        cur_end = 0

        def flush() -> None:
            nonlocal cur_words, cur_redirs, cur_start
            if cur_words or cur_redirs:
                raw = self.cmd[cur_start or 0 : cur_end]
                out.append(_Seg(cur_words, cur_redirs, raw))
            cur_words, cur_redirs, cur_start = [], [], None

        while i < n:
            t = toks[i]
            if t.kind == "op" and t.op in _SEP_OPS:
                flush()
                i += 1
                continue
            at_start = not cur_words and not cur_redirs
            if t.kind == "op" and t.op == "(":
                # subshell, or a function definition "name ( )"
                j = self._match_paren(i)
                start = toks[i].start
                end = toks[j - 1].end if j - 1 < n else len(self.cmd)
                if not at_start and cur_words and j - i == 2:  # f() { … } → opaque through the end
                    self._opaque_text(self.cmd[cur_start or 0 :])
                    return out
                self._opaque_text(self.cmd[start:end])
                cur_words, cur_redirs, cur_start = [], [], None
                i = j
                # redirections directly after the group belong to the opaque unit
                while i < n and toks[i].kind == "redir":
                    i += 1
                continue
            if t.kind == "op" and t.op == ")":
                self._opaque_text(self.cmd[t.start :])
                return out
            if t.kind == "word" and at_start and t.word is not None and not t.word.quoted:
                v = t.word.value
                if v == "{":
                    j = self._match_group(i)
                    end = toks[j - 1].end if j - 1 < n else len(self.cmd)
                    self._opaque_text(self.cmd[t.start : end])
                    i = j
                    while i < n and toks[i].kind == "redir":
                        i += 1
                    continue
                if v == "case":
                    j = i
                    while j < n and not _is_word(toks[j], "esac"):
                        j += 1
                    end = toks[j].end if j < n else len(self.cmd)
                    self._opaque_text(self.cmd[t.start : end])
                    i = j + 1
                    continue
            if cur_start is None:
                cur_start = t.start
            cur_end = t.end
            if t.kind == "word":
                assert t.word is not None
                cur_words.append(t.word)
            elif t.kind == "redir":
                cur_redirs.append(t)
                if t.heredoc_body is not None:
                    pass
            i += 1
        flush()
        return out

    def _match_paren(self, i: int) -> int:
        depth = 0
        for j in range(i, len(self.toks)):
            t = self.toks[j]
            if t.kind == "op" and t.op == "(":
                depth += 1
            elif t.kind == "op" and t.op == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
        return len(self.toks)

    def _match_group(self, i: int) -> int:
        depth = 0
        prev_sep = True
        for j in range(i, len(self.toks)):
            t = self.toks[j]
            if t.kind == "word" and t.word is not None and not t.word.quoted and prev_sep:
                if t.word.value == "{":
                    depth += 1
                elif t.word.value == "}":
                    depth -= 1
                    if depth == 0:
                        return j + 1
            prev_sep = t.kind == "op" or (t.kind == "word" and t.word is not None and t.word.value == "{")
        return len(self.toks)

    def _opaque_text(self, text: str) -> None:
        self.res.opaque = True
        self.all_read_only = False
        _raw_scan(text, self.res)

    # -- helpers ------------------------------------------------------------
    def _mark_opaque(self, seg: _Seg, scan: bool = False, extra: str = "") -> None:
        self.res.opaque = True
        self.all_read_only = False
        if scan or self.no_ro:
            self._scan_once(seg.raw + ("\n" + extra if extra else ""))

    def _scan_once(self, text: str) -> None:
        if text not in self.res.scanned:
            _raw_scan(text, self.res)

    def _target(
        self, w: Word, *, cwd: str | None = None, remove: bool = False, is_dir: bool = False, restore: bool = False
    ) -> None:
        """Record a write target; dynamic targets make the command opaque (and raw-scanned) instead."""
        base = self.cwd if cwd is None else cwd
        w = _home_word(w)
        if w.dynamic:
            # the shell decides the path: scan the segment so `rm -rf $X/.remembra` is still tamper
            self.res.opaque = True
            self.all_read_only = False
            if self.seg_raw:
                self._scan_once(self.seg_raw)
            return
        path = _norm_join(base, w.value)
        if path is None:
            self.res.opaque = True
            self.all_read_only = False
            return
        self.res.writes.add(path)
        if restore:
            self.res.restores.add(path)
        if remove:
            self.res.removes.add(path)
        if is_dir or path == ".":
            self.res.dirs.add(path)

    def _top_target(self, rel: str, *, remove: bool = False, restore: bool = False, is_dir: bool = False) -> None:
        """A target relative to the checkout's top level (resolved by :func:`evaluate` from the command's cwd)."""
        self.all_read_only = False
        if self.cwd is None:
            self.res.opaque = True  # which checkout is unknown
            return
        self.res.top_writes.append((self.cwd, posixpath.normpath(rel), remove, restore, is_dir))

    def _crew_word(self, w: Word) -> bool:
        """``w`` (``$HOME`` resolved) names a crew file: ``.remembra/…``, ``~/.remembra/…`` or ``.git/hooks/…``."""
        w = _home_word(w)
        return not w.dynamic and self._crew_path(w.value)

    def _crew_path(self, value: str) -> bool:
        path = _norm_join(self.cwd or ".", value) or value
        parts = path.split("/")
        if ".remembra" in parts:
            return True
        return any(parts[k] == ".git" and parts[k + 1] == "hooks" for k in range(len(parts) - 1))

    def _tree(self, scopes: Iterable[str]) -> None:
        self.res.tree_writer = True
        for sc in scopes:
            p = _norm_join(self.cwd, sc)
            self.res.tree_scope.add(p if p is not None else ".")
            if p is None:
                self.res.opaque = True

    def _git_op(self, op: str, cwd: str | None) -> None:
        if self.res.git_tree_op is None:
            self.res.git_tree_op = op
            self.res.git_tree_op_cwd = cwd

    # -- one simple command -------------------------------------------------
    def _command(self, seg: _Seg) -> None:
        self.seg_raw = seg.raw
        self.seg_ro = False
        self.no_ro = self.exported_exec
        words = list(seg.words)
        # keywords
        while words and not words[0].quoted and words[0].value in _STRIP_KEYWORDS:
            words.pop(0)
        if words and not words[0].quoted and words[0].value in _OPAQUE_KEYWORDS:
            self._mark_opaque(seg, scan=True)
            return
        # leading assignments
        assigns: list[tuple[str, str, Word]] = []
        while words:
            m = _ASSIGN_RE.fullmatch(words[0].value)
            if m and words[0].name_unquoted:
                assigns.append((m.group(1), m.group(3), words[0]))
                words.pop(0)
            else:
                break
        # wrappers (sudo, env, …) — env may add assignments and unsets
        unsets: list[str] = []
        words, env_bare = self._unwrap(words, assigns, unsets)
        head = words[0].value if words else ""
        self._assign_tamper(assigns, unsets, head)
        if any(_exec_env(name) for name, _v, _w in assigns):
            self.no_ro = True  # e.g. GIT_EXTERNAL_DIFF=… git diff, PAGER=… git log: never read-only
        # redirections
        redirect_write = self._redirections(seg)
        if env_bare and not words:
            self._read_only_seg(seg, redirect_write)
            return
        if not words:
            if not redirect_write and not any(r.heredoc_body is not None for r in seg.redirs):
                # pure assignment: neither read-only nor opaque
                return
            return
        words = self._strip_runner(words)
        if not words:
            self._mark_opaque(seg)
            return
        w0 = words[0]
        if w0.dynamic:
            self._mark_opaque(seg, scan=True)
            return
        name = w0.value
        if "/" in name:
            base = posixpath.basename(name)
            if name.startswith(("./", "../")) or "." in base or not base:
                self._mark_opaque(seg)  # a script
                return
            name = base
        argv = [name] + [w.value for w in words[1:]]
        self.res.argvs.append((self.cwd, argv))
        args = words[1:]
        heredoc = next((r.heredoc_body for r in seg.redirs if r.heredoc_body is not None), None)
        handler = _HANDLERS.get(name)
        if any(w.has_subst for w in args):
            # command substitution in arguments: the command sees text we cannot know
            self._mark_opaque(seg, scan=True)
            if handler in (_h_kill,):
                handler(self, seg, name, args, redirect_write, heredoc)
            return
        if handler is not None:
            handler(self, seg, name, args, redirect_write, heredoc)
            return
        if name in S.BASH_READ_ONLY_COMMANDS or name in _EXTRA_READ_ONLY:
            self._read_only_seg(seg, redirect_write)
            return
        if name in _NEUTRAL_BUILTINS:
            return
        if name in _SHELLS:
            self._h_shell(seg, args, heredoc)
            return
        if name in _INTERPRETERS or re.fullmatch(r"python3?(\.\d+)?", name):
            self._h_interpreter(seg, name, args, heredoc)
            return
        self._mark_opaque(seg)

    def _read_only_seg(self, seg: _Seg, redirect_write: bool) -> None:
        if self.no_ro:
            self._mark_opaque(seg, scan=True)
            return
        if redirect_write:
            self.all_read_only = False
        else:
            self.any_read_only = True
            self.seg_ro = True

    def _redirections(self, seg: _Seg) -> bool:
        wrote = False
        for r in seg.redirs:
            if r.op in ("<", "<<", "<<-", "<<<", "<&"):
                continue
            tgt = r.target
            assert tgt is not None
            if r.op in (">&",) and (tgt.value.isdigit() or tgt.value == "-"):
                continue
            if r.op == "<>":
                pass
            if not tgt.dynamic and tgt.value in _DEV_TARGETS:
                continue
            wrote = True
            self.all_read_only = False
            self._target(tgt)
        return wrote

    def _assign_tamper(self, assigns: list[tuple[str, str, Word]], unsets: list[str], head: str) -> None:
        for name, value, _w in assigns:
            self._check_var(name, value, inline_head=head, exported=False)
        for name in unsets:
            if name in S.CREW_ENV_TAMPER_VARS:
                self.res.tamper.add("env_crew_var")

    def _check_var(self, name: str, value: str, *, inline_head: str | None, exported: bool) -> None:
        if _GIT_CONFIG_ENV_RE.fullmatch(name):
            self.res.tamper.add("hooks_path")  # git reads it as config: core.hooksPath, include.path, alias.*
            return
        if name in S.CREW_ENV_TAMPER_VARS:
            if (
                name == "REMEMBRA_BYPASS"
                and not exported
                and inline_head == "git"
                and _BYPASS_CODE_RE.fullmatch(value) is not None
            ):
                return
            self.res.tamper.add("env_crew_var")
            return
        kind = _HOOK_ENV_TAMPER.get(name)
        if kind is None:
            return
        if name in ("HUSKY", "LEFTHOOK"):
            if value.strip() == "0":
                self.res.tamper.add(kind)
        else:
            self.res.tamper.add(kind)

    def _unwrap(self, words: list[Word], assigns: list[tuple[str, str, Word]], unsets: list[str]) -> tuple[list[Word], bool]:
        env_bare = False
        changed = True
        while words and changed:
            changed = False
            w = words[0]
            if w.quoted or w.dynamic:
                break
            name = w.value
            if name == "sudo":
                i = 1
                while i < len(words) and words[i].value.startswith("-"):
                    if words[i].value in ("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D"):
                        i += 1
                    i += 1
                words = words[i:]
                changed = True
            elif name == "env":
                i = 1
                while i < len(words):
                    v = words[i].value
                    if v in ("-u", "--unset") and i + 1 < len(words):
                        unsets.append(words[i + 1].value)
                        i += 2
                    elif v.startswith("--unset="):
                        unsets.append(v.split("=", 1)[1])
                        i += 1
                    elif v in ("-C", "--chdir", "-S", "--split-string") and i + 1 < len(words):
                        i += 2
                    elif v.startswith("-") and v != "-":
                        i += 1
                    elif _ASSIGN_RE.fullmatch(v) and words[i].name_unquoted:
                        m = _ASSIGN_RE.fullmatch(v)
                        assert m is not None
                        assigns.append((m.group(1), m.group(3), words[i]))
                        i += 1
                    else:
                        break
                words = words[i:]
                env_bare = not words
                changed = True
            elif name in ("command", "builtin"):
                if len(words) > 1 and words[1].value in ("-v", "-V"):
                    return [Word(value="which", raw="which")] + words[2:], False
                words = [x for x in words[1:] if x.value != "-p"] if len(words) > 1 else []
                changed = True
            elif name in ("exec", "nohup"):
                i = 1
                while i < len(words) and words[i].value.startswith("-") and name == "exec":
                    i += 2 if words[i].value == "-a" else 1
                words = words[i:]
                changed = True
            elif name == "time":
                i = 1
                while i < len(words) and words[i].value in ("-p", "-l", "-v"):
                    i += 1
                words = words[i:]
                changed = True
            elif name == "nice":
                i = 1
                while i < len(words) and words[i].value.startswith("-"):
                    i += 2 if words[i].value in ("-n",) else 1
                words = words[i:]
                changed = True
            elif name == "timeout":
                i = 1
                while i < len(words) and words[i].value.startswith("-"):
                    i += 2 if words[i].value in ("-s", "-k", "--signal", "--kill-after") else 1
                words = words[i + 1 :]  # skip the duration
                changed = True
        return words, env_bare

    def _strip_runner(self, words: list[Word]) -> list[Word]:
        """npx / bunx / pnpm exec|dlx / yarn dlx / bun x / npm exec → the package's command name."""
        if not words:
            return words
        v0 = words[0].value
        v1 = words[1].value if len(words) > 1 else ""
        if v0 in ("npx", "bunx"):
            i = 1
        elif (v0, v1) in (("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "dlx"), ("bun", "x"), ("npm", "exec")):
            i = 2
        else:
            return words
        while i < len(words) and words[i].value.startswith("-"):
            v = words[i].value
            if v in ("-c", "--call", "-e"):
                return []  # a command string: opaque
            if v in ("-p", "--package"):
                i += 2
            elif v == "--":
                i += 1
                break
            else:
                i += 1
        rest = words[i:]
        if not rest:
            return rest
        pkg = rest[0].value
        if pkg.startswith("@") and "/" in pkg:
            pkg = pkg.split("/", 1)[1]
        if "@" in pkg[1:]:
            pkg = pkg[: pkg.index("@", 1)]
        return [Word(value=pkg, raw=pkg)] + rest[1:]

    # -- shells and interpreters -------------------------------------------
    def _h_shell(self, seg: _Seg, args: list[Word], heredoc: str | None) -> None:
        self._mark_opaque(seg, scan=True, extra=heredoc or "")

    def _h_interpreter(self, seg: _Seg, name: str, args: list[Word], heredoc: str | None) -> None:
        vals = [a.value for a in args]
        if name.startswith("python") and len(vals) >= 2 and vals[0] == "-m" and vals[1] == "pytest":
            self._read_only_seg(seg, False)
            return
        self._mark_opaque(seg, scan=True, extra=heredoc or "")


# ---------------------------------------------------------------------------
# Command handlers: (parser, seg, name, args, redirect_write, heredoc) -> None
# ---------------------------------------------------------------------------

Handler = Callable[[_Parser, _Seg, str, list[Word], bool, "str | None"], None]


def _split_opts(
    args: list[Word], with_value: Iterable[str] = (), *, stop_at_first_positional: bool = False
) -> tuple[list[str], list[Word], dict[str, str]]:
    """Return (flags, positionals, values). ``with_value`` options consume the next word (or ``--opt=value``)."""
    wv = set(with_value)
    flags: list[str] = []
    pos: list[Word] = []
    values: dict[str, str] = {}
    i = 0
    only_pos = False
    while i < len(args):
        w = args[i]
        v = w.value
        if only_pos or w.quoted and not v.startswith("-") or not v.startswith("-") or v == "-":
            pos.append(w)
            i += 1
            if stop_at_first_positional:
                only_pos = True
            continue
        if v == "--":
            only_pos = True
            i += 1
            continue
        if v.startswith("--") and "=" in v:
            k, val = v.split("=", 1)
            values[k] = val
            flags.append(k)
            i += 1
            continue
        if v in wv:
            values[v] = args[i + 1].value if i + 1 < len(args) else ""
            flags.append(v)
            i += 2
            continue
        flags.append(v)
        i += 1
    return flags, pos, values


def _h_rm(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(args)
    recursive = any(f in ("--recursive",) or (not f.startswith("--") and ("r" in f or "R" in f)) for f in flags)
    if name in ("rmdir",):
        recursive = True
    for w in pos:
        if p._crew_word(w):
            p.res.tamper.add("crew_files_removed")
        p._target(w, remove=True, is_dir=recursive)


def _h_unlink(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _h_rm(p, seg, "unlink", args, rw, hd)


def _h_touch(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args, ("-r", "-t", "-d", "--reference", "--date"))
    for w in pos:
        p._target(w)


def _h_mkdir(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args, ("-m", "--mode"))
    for w in pos:
        p._target(w, is_dir=True)


_CHMOD_OPTS: Final = frozenset(
    {"-R", "-f", "-v", "-h", "-H", "-L", "-P", "-c", "-E", "-C", "-N", "-i", "-I", "-RH", "-RL", "-RP"}
)
_MODE_RE: Final = re.compile(r"[ugoa]*[-+=][rwxXstugo]*(?:,[ugoa]*[-+=][rwxXstugo]*)*|[0-7]{3,4}")


def _h_chmod(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    i = 0
    recursive = False
    have_spec = False
    pos: list[Word] = []
    while i < len(args):
        v = args[i].value
        if v == "--":
            pos.extend(args[i + 1 :])
            break
        if v.startswith("--"):
            if v.startswith("--reference"):
                have_spec = True
            if v == "--recursive":
                recursive = True
            i += 1
            continue
        if v in _CHMOD_OPTS and not have_spec:
            recursive = recursive or "R" in v
            i += 1
            continue
        if not have_spec:
            have_spec = True  # mode (chmod), owner (chown), group (chgrp) — including "-x"
            i += 1
            continue
        pos.append(args[i])
        i += 1
    for w in pos:
        if p._crew_word(w):
            p.res.tamper.add("crew_files_removed")
        p._target(w, is_dir=recursive)


def _h_truncate(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args, ("-s", "-r", "--size", "--reference"))
    for w in pos:
        if p._crew_word(w):
            p.res.tamper.add("crew_files_removed")
        p._target(w)


def _h_dd(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    for w in args:
        if w.value.startswith("of="):
            val = w.value[3:]
            p._target(Word(value=val, raw=val, has_var=w.has_var, has_glob=w.has_glob, has_subst=w.has_subst))


def _h_tee(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args)
    for w in pos:
        if w.value in _DEV_TARGETS:
            continue
        p._target(w)


def _cluster_has(flags: Iterable[str], letter: str) -> bool:
    return any(f.startswith("-") and not f.startswith("--") and letter in f[1:] for f in flags)


_SED_LONG: Final = (
    "--quiet",
    "--silent",
    "--debug",
    "--expression",
    "--file",
    "--follow-symlinks",
    "--in-place",
    "--line-length",
    "--null-data",
    "--zero-terminated",
    "--posix",
    "--regexp-extended",
    "--separate",
    "--sandbox",
    "--unbuffered",
    "--binary",
    "--help",
    "--version",
)
_SED_SIMPLE_CMDS: Final = frozenset("=dDgGhHnNpPxzF")


@dataclass
class _SedEffects:
    writes: list[str] = field(default_factory=list)  # files named by w / W / s///w
    executes: bool = False  # e command or s///e flag
    unknown: bool = False  # a construct this reader does not understand


def _sed_script_effects(script: str) -> _SedEffects:
    """What a sed script does besides printing: ``w``/``W`` and ``s///w`` write files, ``e`` and ``s///e`` run commands.

    A small reader of the GNU/BSD sed grammar (addresses, blocks, labels, text commands, ``s`` and ``y``
    with any delimiter). Anything it cannot read is ``unknown`` (the caller treats the command as opaque).
    """
    out = _SedEffects()
    s = script
    n = len(s)

    def eol(j: int) -> tuple[str, int]:
        k = s.find("\n", j)
        return (s[j:], n) if k < 0 else (s[j:k], k + 1)

    def until(j: int, stops: str) -> int:
        while j < n and s[j] not in stops:
            j += 1
        return j

    def delimited(j: int, d: str) -> int:
        """Index just past the unescaped delimiter ``d`` at or after ``j``; -1 when there is none."""
        while j < n:
            c = s[j]
            if c == "\\":
                j += 2
                continue
            if c == d:
                return j + 1
            j += 1
        return -1

    def filename(j: int) -> int:
        name, nxt = eol(j)
        name = name.strip()
        if not name:
            out.unknown = True
        elif name not in _DEV_TARGETS:
            out.writes.append(name)
        return nxt

    i = 0
    while i < n:
        c = s[i]
        if c in " \t\n;":
            i += 1
            continue
        if c == "#":
            i = eol(i)[1]
            continue
        # address: numbers, $, first~step, addr,+N, /re/I, \cREc, !
        while i < n:
            c = s[i]
            if c.isdigit() or c in "$,~+! \t":
                i += 1
            elif c == "/":
                i = delimited(i + 1, "/")
                if i < 0:
                    out.unknown = True
                    return out
                while i < n and s[i] in "IM":
                    i += 1
            elif c == "\\" and i + 1 < n:
                i = delimited(i + 2, s[i + 1])
                if i < 0:
                    out.unknown = True
                    return out
                while i < n and s[i] in "IM":
                    i += 1
            else:
                break
        if i >= n:
            break
        c = s[i]
        if c in "{}" or c in _SED_SIMPLE_CMDS:
            i += 1
        elif c in "qQlL":
            i = until(i + 1, ";\n}")
        elif c in "btTv:":
            i = until(i + 1, ";\n") if c == ":" else until(i + 1, ";\n}")
        elif c in "aic":
            # one-line text, continued by a trailing backslash
            j = i + 1
            while True:
                line, j = eol(j)
                if not line.endswith("\\") or j >= n:
                    break
            i = j
        elif c in "rR":
            i = eol(i + 1)[1]
        elif c in "wW":
            i = filename(i + 1)
        elif c == "e":
            out.executes = True
            i = eol(i + 1)[1]
        elif c in "sy":
            if i + 1 >= n or s[i + 1] in "\n\\":
                out.unknown = True
                return out
            d = s[i + 1]
            j = delimited(i + 2, d)
            j = delimited(j, d) if j >= 0 else -1
            if j < 0:
                out.unknown = True
                return out
            i = j
            if c == "y":
                continue
            while i < n and s[i] not in ";\n}":
                f = s[i]
                if f == "w":
                    i = filename(i + 1)
                    break
                if f == "e":
                    out.executes = True
                elif not (f.isdigit() or f in "gpiImM \t"):
                    out.unknown = True
                    return out
                i += 1
        else:
            out.unknown = True
            return out
    return out


def _h_sed(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    in_place = False
    sandbox = False
    script_file = False
    scripts: list[Word] = []
    pos: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v == "--":
            pos.extend(args[i + 1 :])
            break
        if v.startswith("--"):
            key, eq, val = v.partition("=")
            opt = _long_opt(key, _SED_LONG)
            if opt == "--in-place":
                in_place = True
            elif opt == "--sandbox":
                sandbox = True
            elif opt in ("--expression", "--file", "--line-length"):
                if not eq:
                    i += 1
                    val = args[i].value if i < len(args) else ""
                if opt == "--expression":
                    src = args[i] if not eq and i < len(args) else w
                    scripts.append(Word(value=val, raw=val, has_var=src.has_var, has_subst=src.has_subst))
                elif opt == "--file":
                    script_file = True
            elif opt is None:
                script_file = True  # an option sed would reject or one we do not know: do not trust it
            i += 1
            continue
        if v.startswith("-") and len(v) > 1:
            letters = v[1:]
            consumed_next = False
            for k, ch in enumerate(letters):
                if ch == "i":
                    in_place = True
                    if k == len(letters) - 1 and i + 1 < len(args) and args[i + 1].value == "" and args[i + 1].quoted:
                        consumed_next = True  # BSD: -i ''
                    break  # GNU: the rest of the cluster is the backup suffix
                if ch in ("e", "f", "l"):
                    rest = letters[k + 1 :]
                    if not rest:
                        consumed_next = True
                        rest = args[i + 1].value if i + 1 < len(args) else ""
                        src = args[i + 1] if i + 1 < len(args) else w
                    else:
                        src = w
                    if ch == "e":
                        scripts.append(Word(value=rest, raw=rest, has_var=src.has_var, has_subst=src.has_subst))
                    elif ch == "f":
                        script_file = True
                    break
            i += 2 if consumed_next else 1
            continue
        pos.append(w)
        i += 1
    if not scripts and not script_file:
        if pos:
            scripts.append(pos[0])
        files = pos[1:]
    else:
        files = pos
    effects = _SedEffects()
    for sw in scripts:
        if sw.dynamic:
            effects.unknown = True
            continue
        one = _sed_script_effects(sw.value)
        effects.writes += one.writes
        effects.executes = effects.executes or one.executes
        effects.unknown = effects.unknown or one.unknown
    if sandbox:
        effects = _SedEffects()  # --sandbox rejects e, w and r: the script can only print
        script_file = False
    unreadable = script_file or effects.executes or effects.unknown
    if unreadable:
        # a script we cannot see or read, or one that runs commands: opaque, raw-scanned
        p._mark_opaque(seg, scan=True, extra="\n".join(sw.value for sw in scripts))
    for target in effects.writes:
        p.all_read_only = False
        p._target(Word(value=target, raw=target))
    if in_place:
        p.all_read_only = False
        for w in files:
            p._target(w)
    elif not unreadable and not effects.writes:
        p._read_only_seg(seg, rw)


def _h_perl(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    in_place = False
    code = False
    pos: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v == "--":
            pos.extend(args[i + 1 :])
            break
        if v.startswith("-") and len(v) > 1:
            letters = v[1:]
            k = 0
            consumed_next = False
            while k < len(letters):
                ch = letters[k]
                if ch == "i":
                    in_place = True
                    break  # rest of the cluster is the backup suffix
                if ch in ("e", "E"):
                    code = True
                    if k == len(letters) - 1:
                        consumed_next = True
                    break
                if ch in ("I", "M", "m", "x", "C", "l", "0", "d", "D"):
                    break
                k += 1
            i += 2 if consumed_next else 1
            continue
        pos.append(w)
        i += 1
    files = pos if code else pos[1:]
    if in_place:
        p.all_read_only = False
        for w in files:
            p._target(w)
        return
    p._mark_opaque(seg, scan=True)


def _h_cp(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, values = _split_opts(args, ("-t", "--target-directory", "-S", "--suffix"))
    target = values.get("-t") or values.get("--target-directory")
    recursive = any(f in ("--recursive", "--archive") or (not f.startswith("--") and any(c in f for c in "rRa")) for f in flags)
    if target is not None:
        p._target(Word(value=target, raw=target), is_dir=True)
        return
    if len(pos) < 2:
        p._mark_opaque(seg)
        return
    dest = pos[-1]
    p._target(dest, is_dir=recursive or len(pos) > 2 or dest.value.endswith("/"))


def _h_mv(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, values = _split_opts(args, ("-t", "--target-directory", "-S", "--suffix"))
    target = values.get("-t") or values.get("--target-directory")
    sources = pos if target is not None else pos[:-1]
    if target is None and len(pos) < 2:
        p._mark_opaque(seg)
        return
    for w in sources:
        if p._crew_word(w):
            p.res.tamper.add("crew_files_removed")
        p._target(w, remove=True)
    if target is not None:
        p._target(Word(value=target, raw=target), is_dir=True)
    else:
        dest = pos[-1]
        p._target(dest, is_dir=len(pos) > 2 or dest.value.endswith("/"))


def _h_install(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, values = _split_opts(args, ("-m", "-o", "-g", "-t", "-S", "-B", "-f", "--mode", "--owner", "--group"))
    if "-d" in flags or "--directory" in flags:
        for w in pos:
            p._target(w, is_dir=True)
        return
    target = values.get("-t")
    if target is not None:
        p._target(Word(value=target, raw=target), is_dir=True)
        return
    if len(pos) < 2:
        p._mark_opaque(seg)
        return
    p._target(pos[-1], is_dir=len(pos) > 2)


def _h_ln(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, values = _split_opts(args, ("-t", "--target-directory", "-S", "--suffix"))
    symbolic = "--symbolic" in flags or _cluster_has(flags, "s")
    target_dir = values.get("-t") or values.get("--target-directory")
    if target_dir is not None or len(pos) > 2:
        # ln SRC… DIR: every link lands in DIR
        d = target_dir if target_dir is not None else pos[-1].value
        srcs = pos if target_dir is not None else pos[:-1]
        p._target(Word(value=d, raw=d), is_dir=True)
        if not symbolic:
            for w in srcs:
                p._target(w)
        return
    if not pos:
        p._mark_opaque(seg)
        return
    src = pos[0]
    link = pos[1] if len(pos) == 2 else Word(value=posixpath.basename(src.value.rstrip("/")) or ".", raw="")
    p._target(link)
    if src.dynamic:
        p.res.opaque = True
        return
    if symbolic and not src.value.startswith(("/", "~")):
        # a relative symlink source is resolved against the link's directory
        link_dir = posixpath.dirname(link.value) or "."
        joined = posixpath.join(link_dir, src.value)
        p._target(Word(value=joined, raw=joined))
    else:
        p._target(src)


_RSYNC_VALUE_OPTS: Final = (
    "-e",
    "--rsh",
    "--exclude",
    "--include",
    "--filter",
    "-f",
    "--files-from",
    "--exclude-from",
    "--include-from",
    "--password-file",
    "--partial-dir",
    "--temp-dir",
    "-T",
    "--backup-dir",
    "--chmod",
    "--chown",
    "--port",
    "--timeout",
    "--rsync-path",
    "-B",
    "--log-file",
)


def _h_rsync(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, values = _split_opts(args, _RSYNC_VALUE_OPTS)
    if "--log-file" in values:
        lf = values["--log-file"]
        p._target(Word(value=lf, raw=lf))
    if len(pos) < 2:
        p._mark_opaque(seg)
        return
    dest = pos[-1]
    if re.match(r"^[^/]*:", dest.value) and not dest.value.startswith("/"):
        p._mark_opaque(seg)  # remote destination
        return
    p._target(dest, is_dir=True)


def _h_patch(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    pos: list[Word] = []
    out: str | None = None
    cwd_change: str | None = None
    i = 0
    while i < len(args):
        v = args[i].value
        if v in ("-i", "--input", "-r", "--reject-file", "-B", "-F", "-V", "-z", "-D", "-p", "-Y", "-Z"):
            i += 2
            continue
        if v in ("-o", "--output"):
            out = args[i + 1].value if i + 1 < len(args) else None
            i += 2
            continue
        if v in ("-d", "--directory"):
            cwd_change = args[i + 1].value if i + 1 < len(args) else None
            i += 2
            continue
        if v.startswith("-"):
            i += 1
            continue
        pos.append(args[i])
        i += 1
    base = _norm_join(p.cwd, cwd_change) if cwd_change else p.cwd
    if out:
        p._target(Word(value=out, raw=out), cwd=base)
    if pos:
        p._target(pos[0], cwd=base)
    elif not out:
        p._mark_opaque(seg)


_SORT_LONG: Final = (
    "--ignore-leading-blanks",
    "--dictionary-order",
    "--ignore-case",
    "--general-numeric-sort",
    "--ignore-nonprinting",
    "--month-sort",
    "--human-numeric-sort",
    "--numeric-sort",
    "--random-sort",
    "--random-source",
    "--reverse",
    "--sort",
    "--version-sort",
    "--batch-size",
    "--check",
    "--compress-program",
    "--debug",
    "--files0-from",
    "--key",
    "--merge",
    "--output",
    "--stable",
    "--buffer-size",
    "--field-separator",
    "--temporary-directory",
    "--parallel",
    "--unique",
    "--zero-terminated",
    "--help",
    "--version",
)
_SORT_LONG_VALUE: Final = frozenset(
    {
        "--random-source",
        "--sort",
        "--batch-size",
        "--compress-program",
        "--files0-from",
        "--key",
        "--output",
        "--buffer-size",
        "--field-separator",
        "--temporary-directory",
        "--parallel",
    }
)


def _h_sort(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    outs: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v == "--":
            break
        if v.startswith("--"):
            key, eq, val = v.partition("=")
            opt = _long_opt(key, _SORT_LONG)
            vw = w
            if opt in _SORT_LONG_VALUE and not eq:
                i += 1
                vw = args[i] if i < len(args) else Word()
                val = vw.value
            if opt == "--compress-program":
                p._mark_opaque(seg, scan=True)  # sort runs this program
                return
            if opt == "--output":
                outs.append(Word(value=val, raw=val, has_var=vw.has_var, has_subst=vw.has_subst, has_glob=vw.has_glob))
            i += 1
            continue
        if v.startswith("-") and len(v) > 1:
            letters = v[1:]
            for k, ch in enumerate(letters):
                if ch in "ktoST":
                    rest = letters[k + 1 :]
                    vw = w
                    if not rest:
                        i += 1
                        vw = args[i] if i < len(args) else Word()
                        rest = vw.value
                    if ch == "o":
                        outs.append(Word(value=rest, raw=rest, has_var=vw.has_var, has_subst=vw.has_subst, has_glob=vw.has_glob))
                    break
            i += 1
            continue
        i += 1
    if not outs:
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    for o in outs:
        p._target(o)


def _h_uniq(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    """``uniq [OPTION]… [INPUT [OUTPUT]]``: the second operand is written."""
    _flags, pos, _ = _split_opts(args, ("-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"))
    if len(pos) >= 2 and pos[1].value not in ("-", *_DEV_TARGETS):
        p.all_read_only = False
        p._target(pos[1])
        return
    p._read_only_seg(seg, rw)


def _h_tree(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    """``tree -o FILE`` writes its listing to FILE."""
    i = 0
    outs: list[Word] = []
    while i < len(args):
        v = args[i].value
        if v in ("-L", "-P", "-I", "-H", "-T", "--filelimit", "--timefmt", "--charset", "--sort", "--infile"):
            i += 2
            continue
        if v == "-o" and i + 1 < len(args):
            outs.append(args[i + 1])
            i += 2
            continue
        i += 1
    if not outs:
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    for o in outs:
        p._target(o)


def _h_pager(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    """``less``/``more``: ``-o``/``-O``/``--log-file`` write a log file; ``+…!cmd`` runs a shell command."""
    outs: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v.startswith("+") and "!" in v:
            p._mark_opaque(seg, scan=True)
            return
        key, eq, val = v.partition("=")
        if key.lower() == "--log-file":
            if eq:
                outs.append(Word(value=val, raw=val, has_var=w.has_var, has_subst=w.has_subst, has_glob=w.has_glob))
            elif i + 1 < len(args):
                outs.append(args[i + 1])
                i += 1
        elif v[:2] in ("-o", "-O") and not v.startswith("--"):
            if len(v) > 2:
                outs.append(Word(value=v[2:], raw=v[2:], has_var=w.has_var, has_subst=w.has_subst, has_glob=w.has_glob))
            elif i + 1 < len(args):
                outs.append(args[i + 1])
                i += 1
        i += 1
    if not outs:
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    for o in outs:
        p._target(o)


def _h_search(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    """``rg --pre CMD``, ``fd -x/-X CMD`` and ``ag --pager CMD`` run commands; plain searches are read-only."""
    runs = {
        "rg": ("--pre",),
        "fd": ("-x", "--exec", "-X", "--exec-batch"),
        "ag": ("--pager",),
    }[name]
    for w in args:
        key = w.value.split("=", 1)[0]
        if key == "--":
            break
        if key in runs or (key.startswith("--") and len(key) > 3 and any(r.startswith(key) for r in runs if r.startswith("--"))):
            p._mark_opaque(seg, scan=True)
            return
    p._read_only_seg(seg, rw)


_CURL_VALUE_LETTERS: Final = frozenset("oHdurXAebcDKmwyYzECFT")


def _h_curl(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    outs: list[str] = []
    i = 0
    while i < len(args):
        v = args[i].value
        if v in ("--output",) and i + 1 < len(args):
            outs.append(args[i + 1].value)
            i += 2
            continue
        if v.startswith("--output="):
            outs.append(v.split("=", 1)[1])
            i += 1
            continue
        if v.startswith("--"):
            i += 1
            continue
        if v.startswith("-") and len(v) > 1:
            letters = v[1:]
            consumed = False
            for k, ch in enumerate(letters):
                if ch in _CURL_VALUE_LETTERS:
                    val = letters[k + 1 :]
                    if not val and i + 1 < len(args):
                        val = args[i + 1].value
                        consumed = True
                    if ch == "o":
                        outs.append(val)
                    break
            i += 2 if consumed else 1
            continue
        i += 1
    if not outs:
        p._mark_opaque(seg)
        return
    for o in outs:
        p._target(Word(value=o, raw=o))


def _h_wget(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    outs: list[str] = []
    dirs: list[str] = []
    for i, w in enumerate(args):
        v = w.value
        if v in ("-O", "--output-document") and i + 1 < len(args):
            outs.append(args[i + 1].value)
        elif v.startswith("--output-document="):
            outs.append(v.split("=", 1)[1])
        elif v in ("-P", "--directory-prefix") and i + 1 < len(args):
            dirs.append(args[i + 1].value)
    if not outs and not dirs:
        p._mark_opaque(seg)
        return
    for o in outs:
        if o != "-":
            p._target(Word(value=o, raw=o))
    for d in dirs:
        p._target(Word(value=d, raw=d), is_dir=True)


def _h_cd(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    pos = [a for a in args if not (a.value.startswith("-") and a.value not in ("-",) and not a.quoted)]
    pos = [a for a in pos if a.value != "--"]
    if name == "popd":
        p.cwd = p.dirstack.pop() if p.dirstack else None
        return
    if not pos:
        target: str | None = "~"
    elif pos[0].dynamic or pos[0].value == "-":
        target = None
    else:
        target = _norm_join(p.cwd, pos[0].value)
    if name == "pushd":
        p.dirstack.append(p.cwd)
    p.cwd = target
    if target is None:
        p.all_read_only = False


def _h_export(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    for w in args:
        m = _ASSIGN_RE.fullmatch(w.value)
        if m:
            p._check_var(m.group(1), m.group(3), inline_head=None, exported=True)
            if _exec_env(m.group(1)):
                # PAGER, GIT_*, PATH …: later commands run other code; they are never read-only
                p.exported_exec = True
                p._mark_opaque(seg, scan=True)


def _h_unset(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    for w in args:
        if w.value in S.CREW_ENV_TAMPER_VARS:
            p.res.tamper.add("env_crew_var")


# the names crewd runs under (console script and module): a pkill/killall pattern that is part of one of
# them (``pkill -f remembra``, ``killall remembra-crew``) stops crewd as surely as naming it in full
_CREWD_PROCESS_NAMES: Final = ("remembra-crewd", "remembra.relay.crew.crewd")


def _kill_targets(args: list[Word]) -> set[int]:
    """Literal pids a ``kill`` sends to (``kill [-s SIG | -SIG | -n N] [--] pid|-pgid …``)."""
    out: set[int] = set()
    vals = [w.value for w in args if not w.dynamic]
    i, options = 0, True
    while i < len(vals):
        v = vals[i]
        i += 1
        if options and v == "--":
            options = False
            continue
        if options and v in ("-s", "-n"):
            i += 1  # the signal
            continue
        if options and v.startswith("-") and not out and (not v[1:].lstrip("-").isdigit() or i == 1):
            continue  # -9, -KILL, -SIGTERM, -l: an option (a leading -N is always the signal)
        options = False
        if re.fullmatch(r"-?\d{1,10}", v):
            n = int(v)
            out.add(-1 if n == -1 else abs(n))
    return out


def _h_kill(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    if any("crewd" in w.value for w in args):
        p.res.tamper.add("crewd_kill")
        p.all_read_only = False
        return
    if name in ("pkill", "killall"):
        patterns = [w.value for w in args if not w.dynamic and w.value and not w.value.startswith("-")]
        if any(len(v) >= 4 and any(v.lower() in n for n in _CREWD_PROCESS_NAMES) for v in patterns):
            p.res.tamper.add("crewd_kill")
            p.all_read_only = False
            return
    elif name == "kill":
        p.res.kill_pids |= _kill_targets(args)
    p._mark_opaque(seg)


def _h_launchctl(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    vals = [w.value for w in args]
    if vals and vals[0] in ("bootout", "unload", "remove", "kill", "disable", "stop") and any("crewd" in v for v in vals[1:]):
        p.res.tamper.add("crewd_kill")
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_systemctl(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    vals = [w.value for w in args if not w.value.startswith("-")]
    if vals and vals[0] in ("stop", "disable", "kill", "mask", "restart") and any("remembra-crewd" in v for v in vals[1:]):
        p.res.tamper.add("crewd_kill")
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_eval(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    p._mark_opaque(seg, scan=True)


def _h_opaque_code(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    p._mark_opaque(seg, scan=True, extra=hd or "")


def _h_find(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    vals = [w.value for w in args]
    if any(v in ("-exec", "-execdir", "-ok", "-okdir", "-delete") or v.startswith("-fprint") or v == "-fls" for v in vals):
        p._mark_opaque(seg, scan=True)
        return
    p._read_only_seg(seg, rw)


def _formatter_scope(p: _Parser, seg: _Seg, pos: list[Word]) -> None:
    """Directory arguments → tree scope; file arguments → plain writes; glob/variable/none → whole checkout."""
    scopes: list[str] = []
    for w in pos:
        v = w.value
        if w.dynamic or any(c in v for c in "*?[{"):
            scopes.append(".")
        elif v.endswith("/..."):
            scopes.append(v[: -len("/...")] or ".")
        elif v in (".", "./") or not _has_ext(v):
            scopes.append(v)
        else:
            p._target(w)
    if scopes:
        p._tree(scopes)
    elif not pos:
        p._tree(["."])
    p.all_read_only = False


_PRETTIER_VALUE_OPTS: Final = (
    "--config",
    "--ignore-path",
    "--plugin",
    "--parser",
    "--log-level",
    "--loglevel",
    "--cache-location",
    "--stdin-filepath",
    "--tab-width",
    "--print-width",
    "--end-of-line",
    "--trailing-comma",
    "--arrow-parens",
    "--prose-wrap",
    "--quote-props",
    "--html-whitespace-sensitivity",
    "--embedded-language-formatting",
    "--cursor-offset",
    "--range-start",
    "--range-end",
    "--config-precedence",
)
_ESLINT_VALUE_OPTS: Final = (
    "-c",
    "--config",
    "--ext",
    "--rulesdir",
    "-f",
    "--format",
    "--ignore-path",
    "--ignore-pattern",
    "--resolve-plugins-relative-to",
    "--parser",
    "--parser-options",
    "--plugin",
    "--rule",
    "--env",
    "--global",
    "--max-warnings",
    "--cache-location",
    "--cache-strategy",
    "--fix-type",
    "--stdin-filename",
    "--report-unused-disable-directives-severity",
)


def _h_prettier(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(args, _PRETTIER_VALUE_OPTS)
    if not any(f in ("--write", "-w") for f in flags):
        p._read_only_seg(seg, rw)
        return
    _formatter_scope(p, seg, pos)


def _h_eslint(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, values = _split_opts(args, (*_ESLINT_VALUE_OPTS, "-o", "--output-file"))
    for key in ("-o", "--output-file"):
        if key in values:
            p._target(Word(value=values[key], raw=values[key]))
    if "--fix" not in flags and "--fix-dry-run" not in flags:
        if "-o" in values or "--output-file" in values:
            p.all_read_only = False
            return
        p._read_only_seg(seg, rw)
        return
    if "--fix-dry-run" in flags and "--fix" not in flags:
        p._read_only_seg(seg, rw)
        return
    _formatter_scope(p, seg, pos)


def _h_biome(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(args, ("--config-path", "--max-diagnostics", "--log-level", "--log-kind", "--stdin-file-path"))
    sub = pos[0].value if pos else ""
    writes = any(f in ("--write", "--apply", "--apply-unsafe", "--fix", "--unsafe") for f in flags)
    if sub not in ("format", "check", "lint", "ci") or not writes or sub == "ci":
        p._read_only_seg(seg, rw)
        return
    _formatter_scope(p, seg, pos[1:])


def _h_ruff(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(
        args,
        (
            "--config",
            "--select",
            "--ignore",
            "--extend-select",
            "--exclude",
            "--line-length",
            "--target-version",
            "--output-format",
        ),
    )
    sub = pos[0].value if pos else ""
    if sub == "format" and not any(f in ("--check", "--diff") for f in flags):
        _formatter_scope(p, seg, pos[1:])
        return
    if sub == "check" and any(f in ("--fix", "--unsafe-fixes") for f in flags) and "--diff" not in flags:
        _formatter_scope(p, seg, pos[1:])
        return
    if sub in ("check", "format", "rule", "version", "linter", "") or sub.startswith("-"):
        p._read_only_seg(seg, rw)
        return
    p._mark_opaque(seg)


def _h_black(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(
        args, ("-l", "--line-length", "-t", "--target-version", "--config", "--include", "--exclude", "--extend-exclude")
    )
    if any(f in ("--check", "--diff") for f in flags):
        p._read_only_seg(seg, rw)
        return
    _formatter_scope(p, seg, pos)


def _h_gofmt(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    flags, pos, _ = _split_opts(args, ("-r",))
    if "-w" not in flags:
        p._read_only_seg(seg, rw)
        return
    _formatter_scope(p, seg, pos)


def _h_go(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    sub = args[0].value if args else ""
    if sub in ("test", "vet", "version", "env", "list", "doc"):
        p._read_only_seg(seg, rw)
        return
    if sub == "fmt":
        _flags, pos, _ = _split_opts(args[1:])
        scopes = []
        for w in pos:
            v = w.value
            v = v[: -len("/...")] if v.endswith("/...") else v
            scopes.append(v or ".")
        p._tree(scopes or ["."])
        p.all_read_only = False
        return
    if sub == "generate":
        p._tree(["."])
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_cargo(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    sub = args[0].value if args else ""
    vals = [w.value for w in args[1:]]
    if sub in ("test", "check", "tree", "metadata", "version") or (sub == "fmt" and "--check" in vals):
        p._read_only_seg(seg, rw)
        return
    if sub == "fmt" or (sub == "clippy" and "--fix" in vals):
        p._tree(["."])
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_prisma(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args, ("--schema", "--name", "-n"))
    subs = [w.value for w in pos]
    if subs[:2] == ["migrate", "dev"]:
        p._target(Word(value="prisma/migrations", raw="prisma/migrations"), is_dir=True)
        return
    if subs[:1] == ["generate"]:
        p._tree(["."])
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_supabase(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    subs = [w.value for w in args if not w.value.startswith("-")]
    if subs[:2] == ["gen", "types"]:
        p._tree(["."])
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _h_openapi(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, values = _split_opts(
        args, ("-i", "--input-spec", "-g", "--generator-name", "-o", "--output", "-c", "--config", "-t", "--template-dir")
    )
    if not pos or pos[0].value != "generate":
        p._mark_opaque(seg)
        return
    out = values.get("-o") or values.get("--output")
    p._tree([out] if out else ["."])
    p.all_read_only = False


def _h_codegen(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    p._tree(["."])
    p.all_read_only = False


def _h_tsc(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    vals = [w.value for w in args]
    if "--noEmit" in vals or "--version" in vals or "-v" in vals:
        p._read_only_seg(seg, rw)
        return
    p._mark_opaque(seg)


def _pm_script(p: _Parser, seg: _Seg, script: str, rw: bool) -> None:
    low = script.lower()
    if low in _PM_TEST_SCRIPTS or low.startswith("test:"):
        p._read_only_seg(seg, rw)
        return
    if any(word in low for word in S.BASH_TREE_SCRIPT_WORDS):
        p._tree(["."])
        p.all_read_only = False
        return
    p._mark_opaque(seg)


_NPM_BUILTINS: Final = frozenset(
    {
        "install",
        "i",
        "ci",
        "add",
        "uninstall",
        "remove",
        "rm",
        "update",
        "up",
        "upgrade",
        "init",
        "create",
        "publish",
        "link",
        "unlink",
        "pack",
        "prune",
        "dedupe",
        "audit",
        "outdated",
        "ls",
        "list",
        "why",
        "info",
        "view",
        "config",
        "set",
        "get",
        "cache",
        "import",
        "patch",
        "env",
        "store",
        "rebuild",
        "setup",
        "self-update",
        "dlx",
        "exec",
        "x",
        "start",
        "restart",
        "stop",
        "version",
        "whoami",
        "login",
        "logout",
        "workspace",
        "workspaces",
        "global",
        "bin",
        "root",
        "prefix",
        "doctor",
        "help",
    }
)


def _h_pm(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    # skip leading options (pnpm -C dir, yarn --cwd dir, npm --prefix dir, -w, --filter x)
    i = 0
    while i < len(args) and args[i].value.startswith("-"):
        i += 2 if args[i].value in ("-C", "--cwd", "--prefix", "--filter", "-F", "--dir", "-w", "--workspace") else 1
    rest = args[i:]
    if not rest:
        p._mark_opaque(seg)  # bare "yarn" / "pnpm" installs
        return
    sub = rest[0].value
    if sub in ("test", "t", "tst"):
        p._read_only_seg(seg, rw)
        return
    if sub in ("run", "run-script", "rum", "urn"):
        if len(rest) < 2:
            p._read_only_seg(seg, rw)  # lists scripts
            return
        _pm_script(p, seg, rest[1].value, rw)
        return
    if name == "npm":
        p._mark_opaque(seg)
        return
    if sub in _NPM_BUILTINS:
        p._mark_opaque(seg)
        return
    if name == "bun" and ("/" in sub or _has_ext(sub)):
        p._mark_opaque(seg)  # bun <file>
        return
    _pm_script(p, seg, sub, rw)


# -- git ---------------------------------------------------------------------

_GIT_COMMIT_VALUE_SHORT: Final = frozenset("mFCct")
_GIT_COMMIT_OPTIONAL_ATTACHED: Final = frozenset("Su")
_GIT_COMMIT_VALUE_LONG: Final = frozenset(
    {
        "--message",
        "--file",
        "--author",
        "--date",
        "--template",
        "--reuse-message",
        "--reedit-message",
        "--fixup",
        "--squash",
        "--cleanup",
        "--trailer",
        "--pathspec-from-file",
    }
)
_GIT_READ_ONLY_SUBS: Final = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "rev-parse",
        "blame",
        "fetch",
        "ls-files",
        "ls-tree",
        "cat-file",
        "describe",
        "shortlog",
        "reflog",
        "grep",
        "merge-base",
        "rev-list",
        "whatchanged",
        "show-ref",
        "for-each-ref",
        "name-rev",
        "count-objects",
        "check-ignore",
        "version",
        "help",
        "var",
    }
)


# Subcommands that run client-side hooks (pre-commit, commit-msg, pre-merge-commit, pre-push,
# applypatch-msg, pre-rebase): an abbreviated --no-verify on any of them skips the hooks.
_GIT_HOOK_SUBS: Final = frozenset({"commit", "push", "merge", "am", "rebase", "pull", "cherry-pick", "revert"})
# Every git subcommand the parser knows; anything else is an alias or an external git-<name>.
_GIT_KNOWN_SUBS: Final = frozenset(
    {
        *_GIT_READ_ONLY_SUBS,
        *_GIT_HOOK_SUBS,
        "add",
        "annotate",
        "apply",
        "archive",
        "backfill",
        "bisect",
        "branch",
        "bundle",
        "check-attr",
        "check-mailmap",
        "check-ref-format",
        "checkout",
        "checkout-index",
        "cherry",
        "clean",
        "clone",
        "column",
        "commit-graph",
        "commit-tree",
        "config",
        "credential",
        "diff-files",
        "diff-index",
        "diff-tree",
        "difftool",
        "fast-export",
        "fast-import",
        "fetch-pack",
        "filter-branch",
        "fmt-merge-msg",
        "for-each-repo",
        "format-patch",
        "fsck",
        "gc",
        "hash-object",
        "hook",
        "index-pack",
        "init",
        "interpret-trailers",
        "lfs",
        "ls-remote",
        "maintenance",
        "merge-file",
        "merge-index",
        "merge-tree",
        "mergetool",
        "mktag",
        "mktree",
        "multi-pack-index",
        "mv",
        "notes",
        "pack-objects",
        "pack-refs",
        "patch-id",
        "prune",
        "range-diff",
        "read-tree",
        "remote",
        "repack",
        "replace",
        "replay",
        "request-pull",
        "rerere",
        "reset",
        "restore",
        "rm",
        "send-email",
        "show-branch",
        "sparse-checkout",
        "stash",
        "stripspace",
        "submodule",
        "switch",
        "symbolic-ref",
        "tag",
        "update-index",
        "update-ref",
        "verify-commit",
        "verify-tag",
        "worktree",
        "write-tree",
    }
)
# Config whose value git runs as a command (or that writes trace files / enables other transports).
_GIT_EXEC_KEYS: Final = frozenset(
    {
        "core.fsmonitor",
        "core.pager",
        "core.sshcommand",
        "core.editor",
        "core.askpass",
        "core.gitproxy",
        "core.alternaterefscommand",
        "core.worktree",
        "diff.external",
        "sequence.editor",
        "gpg.program",
        "credential.helper",
        "uploadpack.packobjectshook",
        "interactive.difffilter",
        "web.browser",
    }
)
_GIT_EXEC_KEY_RE: Final = re.compile(
    r"(?:pager|trace2|protocol)\..+"
    r"|diff\..+\.(?:textconv|command)|merge\..+\.driver|filter\..+\.(?:clean|smudge|process)"
    r"|(?:difftool|mergetool|browser|man)\..+\.(?:cmd|path)|credential\..+\.helper|gpg\..+\.program"
    r"|submodule\..+\.update|hook\..+\.command|remote\..+\.(?:uploadpack|receivepack|vcs)"
)
# A command value that is only a program name plus flags (``cat``, ``less -R``, ``code --wait``).
_GIT_SIMPLE_COMMAND_RE: Final = re.compile(r"[A-Za-z0-9_.+-]+(?: +-{1,2}[A-Za-z0-9_=.-]+)*")
_GIT_CHECKOUT_LONG: Final = (
    "--quiet",
    "--progress",
    "--no-progress",
    "--force",
    "--ours",
    "--theirs",
    "--track",
    "--no-track",
    "--guess",
    "--no-guess",
    "--detach",
    "--orphan",
    "--ignore-skip-worktree-bits",
    "--merge",
    "--conflict",
    "--patch",
    "--ignore-other-worktrees",
    "--overwrite-ignore",
    "--no-overwrite-ignore",
    "--recurse-submodules",
    "--no-recurse-submodules",
    "--overlay",
    "--no-overlay",
    "--pathspec-from-file",
    "--pathspec-file-nul",
)
_GIT_RESET_LONG: Final = (
    "--quiet",
    "--no-quiet",
    "--refresh",
    "--no-refresh",
    "--mixed",
    "--soft",
    "--hard",
    "--merge",
    "--keep",
    "--recurse-submodules",
    "--no-recurse-submodules",
    "--patch",
    "--intent-to-add",
    "--pathspec-from-file",
    "--pathspec-file-nul",
)
_GIT_CLEAN_LONG: Final = ("--force", "--dry-run", "--quiet", "--interactive", "--exclude")


def _is_no_verify(value: str) -> bool:
    """``--no-verify`` or any abbreviation git's option parser would accept for it (``--no-veri``, ``--no-verif``)."""
    key = value.split("=", 1)[0]
    return len(key) >= len("--no-v") and "--no-verify".startswith(key)


def _git_no_verify(args: list[Word], short_n_is_tamper: bool) -> bool:
    """``--no-verify`` (or an abbreviation) in options; ``-n`` in a short-flag cluster for commit (a value flag ends it)."""
    i = 0
    while i < len(args):
        v = args[i].value
        if v == "--":
            return False
        if _is_no_verify(v):
            return True
        if v in _GIT_COMMIT_VALUE_LONG:
            i += 2
            continue
        if v.startswith("-") and not v.startswith("--") and len(v) > 1 and short_n_is_tamper:
            letters = v[1:]
            consumed_next = False
            for k, ch in enumerate(letters):
                if ch == "n":
                    return True
                if ch in _GIT_COMMIT_VALUE_SHORT:
                    if k == len(letters) - 1:
                        consumed_next = True
                    break
                if ch in _GIT_COMMIT_OPTIONAL_ATTACHED:
                    break
            i += 2 if consumed_next else 1
            continue
        i += 1
    return False


def _git_key_class(key: str) -> str:
    """``hooks`` (hooks path, includes), ``alias``, ``exec`` (the value runs as a command) or ``other``."""
    k = key.strip().lower()
    if k == "core.hookspath" or k == "include.path" or (k.startswith("includeif.") and k.endswith(".path")):
        return "hooks"
    if k.startswith("alias."):
        return "alias"
    if k in _GIT_EXEC_KEYS or _GIT_EXEC_KEY_RE.fullmatch(k):
        return "exec"
    return "other"


def _git_subcommand(argv: Sequence[str]) -> str | None:
    """The subcommand of ``git …`` values (global options skipped)."""
    i = 0
    while i < len(argv):
        v = argv[i]
        if v in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"):
            i += 2
            continue
        if v.startswith("-"):
            i += 1
            continue
        return v
    return None


def _git_alias_tamper(value: str | None) -> bool:
    """An alias that could run a hook-skipping command unseen: a shell alias, an alias of a hook-running
    subcommand (``git ci -n`` then skips the hooks) or one whose own text is tamper."""
    if value is None:
        return True  # the value comes from the environment
    text = value.strip()
    if text.startswith("!"):
        return True
    parsed = parse_bash_full("git " + text)
    if parsed.tamper or parsed.opaque and not parsed.argvs:
        return True
    sub = _git_subcommand(parsed.argvs[0][1][1:]) if parsed.argvs else None
    return sub in _GIT_HOOK_SUBS or sub == "commit-tree"


def _git_config_override(p: _Parser, pair: str, *, known: bool) -> None:
    """One ``-c key=value`` / ``--config-env=key=VAR`` for a single git run."""
    key, _eq, value = pair.partition("=")
    cls = _git_key_class(key)
    if cls == "hooks":
        p.res.tamper.add("hooks_path")
    elif cls == "alias" and _git_alias_tamper(value if known else None):
        p.res.tamper.add("no_verify")


def _h_git(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    cwd = p.cwd
    configured = False
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v == "-C" and i + 1 < len(args):
            nxt = args[i + 1]
            cwd = None if nxt.dynamic else _norm_join(cwd, nxt.value)
            i += 2
            continue
        if v == "-c" and i + 1 < len(args):
            _git_config_override(p, args[i + 1].value, known=not args[i + 1].dynamic)
            configured = True
            i += 2
            continue
        if v.startswith("--config-env"):
            val = v.split("=", 1)[1] if "=" in v else (args[i + 1].value if i + 1 < len(args) else "")
            _git_config_override(p, val.split("=", 1)[0], known=False)
            configured = True
            i += 1 if "=" in v else 2
            continue
        if v.startswith("--exec-path="):
            configured = True  # git runs its subcommands from that directory
            i += 1
            continue
        if v in ("--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            if v == "--work-tree":
                cwd = None if args[i + 1].dynamic else _norm_join(cwd, args[i + 1].value)
            i += 2
            continue
        if v.startswith("--work-tree="):
            cwd = _norm_join(cwd, v.split("=", 1)[1])
            i += 1
            continue
        if v.startswith("-"):
            i += 1
            continue
        break
    if configured:
        # a -c value can be a command git runs (core.fsmonitor, diff.external, core.pager …):
        # never read-only, and scanned like opaque code
        p.no_ro = True
        p._scan_once(seg.raw)
    if i >= len(args):
        p._read_only_seg(seg, rw)
        return
    sub = args[i].value
    rest = args[i + 1 :]
    vals = [w.value for w in rest]
    saved = p.cwd
    p.cwd = cwd
    try:
        _git_sub(p, seg, sub, rest, vals, cwd, rw)
    finally:
        p.cwd = saved


def _git_read_only_effects(p: _Parser, seg: _Seg, sub: str, rest: list[Word]) -> bool:
    """Options of read-only subcommands that write files or run commands. True when the segment was classified."""
    outs: list[Word] = []
    runs = False
    i = 0
    while i < len(rest):
        w = rest[i]
        v = w.value
        if v == "--":
            break
        if v.startswith("--"):
            key, eq, val = v.partition("=")
            if sub in ("diff", "log", "show", "whatchanged", "shortlog") and len(key) >= 5 and "--output".startswith(key):
                vw = w
                if not eq:
                    i += 1
                    vw = rest[i] if i < len(rest) else Word()
                    val = vw.value
                outs.append(Word(value=val, raw=val, has_var=vw.has_var, has_subst=vw.has_subst, has_glob=vw.has_glob))
            elif (
                (sub == "grep" and len(key) >= 4 and "--open-files-in-pager".startswith(key))
                or (sub == "fetch" and len(key) >= 5 and "--upload-pack".startswith(key))
                or (sub == "help" and key in ("--web", "--info"))
            ):
                runs = True
        elif v.startswith("-") and len(v) > 1:
            if sub == "grep" and "O" in v[1:]:
                runs = True  # -O[<pager>] opens the matches in a pager command
            elif sub == "help" and any(c in v[1:] for c in "wi"):
                runs = True
        i += 1
    if runs:
        p._mark_opaque(seg, scan=True)
    if outs:
        p.all_read_only = False
        for o in outs:
            p._target(o)
    return runs or bool(outs)


def _git_sub(p: _Parser, seg: _Seg, sub: str, rest: list[Word], vals: list[str], cwd: str | None, rw: bool) -> None:
    def ro() -> None:
        p._read_only_seg(seg, rw)

    unknown = sub not in _GIT_KNOWN_SUBS
    if (sub in _GIT_HOOK_SUBS or unknown) and _git_no_verify(rest, short_n_is_tamper=sub == "commit"):
        p.res.tamper.add("no_verify")
    if unknown:
        p._mark_opaque(seg, scan=True)  # an alias or an external git-<name>: we cannot see what it runs
        return
    if sub in _GIT_READ_ONLY_SUBS:
        if not _git_read_only_effects(p, seg, sub, rest):
            ro()
        return
    if sub == "branch":
        listing_only = all(
            v
            in (
                "-a",
                "-r",
                "-v",
                "-vv",
                "--list",
                "-l",
                "--all",
                "--remotes",
                "--show-current",
                "--verbose",
                "--no-color",
                "--color",
            )
            or v.startswith(("--sort", "--format", "--contains", "--merged", "--no-merged", "--points-at", "--column"))
            for v in vals
        )
        ro() if listing_only else p._mark_opaque(seg)
        return
    if sub == "tag":
        ro() if (not vals or any(v in ("-l", "--list", "-n") or v.startswith("-n") for v in vals)) else p._mark_opaque(seg)
        return
    if sub == "remote":
        ro() if (not vals or vals[0] in ("-v", "--verbose", "show", "get-url")) else p._mark_opaque(seg)
        return
    if sub == "worktree":
        _git_worktree(p, seg, rest, vals)
        return
    if sub == "config":
        _git_config(p, seg, vals, rw)
        return
    if sub == "stash":
        first = next((v for v in vals if not v.startswith("-")), "")
        if first in ("list", "show"):
            ro()
        else:
            p._git_op("stash", cwd)
            p.all_read_only = False
        return
    if sub == "clean":
        opts = [_long_opt(v.split("=", 1)[0], _GIT_CLEAN_LONG) if v.startswith("--") else v for v in vals if v.startswith("-")]
        if any(f == "--dry-run" or (f and not f.startswith("--") and "n" in f[1:]) for f in opts):
            ro()
        elif any(f == "--force" or (f and not f.startswith("--") and "f" in f[1:]) for f in opts):
            p._git_op("clean", cwd)
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    if sub == "checkout":
        _git_checkout(p, seg, rest, cwd)
        return
    if sub == "switch":
        p._git_op("switch", cwd)
        p.all_read_only = False
        return
    if sub == "restore":
        flags, pos, values = _split_opts(rest, ("-s", "--source", "--conflict", "--pathspec-from-file"))
        from_ref = "-s" in values or "--source" in values or any(f.startswith("-s") and len(f) > 2 for f in flags)
        if "--pathspec-from-file" in values:
            p._top_target(".", restore=not from_ref, is_dir=True)  # the listed paths can be anywhere in the tree
            return
        if not pos:
            p._mark_opaque(seg)
            return
        _git_pathspecs(p, pos, restore=not from_ref)
        return
    if sub == "rm":
        flags, pos, values = _split_opts(rest, ("--pathspec-from-file",))
        if any(f in ("-n", "--dry-run") for f in flags):
            ro()
            return
        recursive = any(f == "-r" or (not f.startswith("--") and "r" in f[1:]) for f in flags)
        for w in pos:
            if p._crew_word(w):
                p.res.tamper.add("crew_files_removed")
        if "--pathspec-from-file" in values:
            p._top_target(".", remove=True, is_dir=True)
            return
        if not pos:
            p._mark_opaque(seg)
            return
        _git_pathspecs(p, pos, remove=True, is_dir=recursive)
        return
    if sub == "mv":
        _flags, pos, _ = _split_opts(rest)
        if len(pos) < 2:
            p._mark_opaque(seg)
            return
        for w in pos[:-1]:
            if p._crew_word(w):
                p.res.tamper.add("crew_files_removed")
            p._target(w, remove=True)
        p._target(pos[-1])
        return
    if sub == "reset":
        reset_opts = {_long_opt(v.split("=", 1)[0], _GIT_RESET_LONG) for v in vals if v.startswith("--")}
        if reset_opts & {"--hard", "--merge", "--keep"}:
            p._git_op("reset_hard", cwd)  # --merge and --keep also rewrite working-tree files
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    if sub == "read-tree":
        if any(v == "-u" or (v.startswith("-") and not v.startswith("--") and "u" in v[1:]) for v in vals):
            p._git_op("reset_hard", cwd)  # -u rewrites the working tree from the read index
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    if sub == "checkout-index":
        _git_checkout_index(p, seg, rest, cwd)
        return
    if sub == "format-patch":
        _flags, _pos, values = _split_opts(rest, ("-o", "--output-directory"))
        out = values.get("-o") or values.get("--output-directory")
        if out is not None:
            p._target(Word(value=out, raw=out), is_dir=True)
        elif "--stdout" in vals:
            ro()
        else:
            p._mark_opaque(seg)  # writes numbered patch files into the current directory
        return
    if sub == "commit-tree":
        p.res.tamper.add("no_verify")  # a commit object made without running any hook
        p._mark_opaque(seg)
        return
    if sub in ("rebase", "merge", "pull", "cherry-pick"):
        p._git_op(sub.replace("-", "_"), cwd)
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _git_worktree(p: _Parser, seg: _Seg, rest: list[Word], vals: list[str]) -> None:
    """``worktree remove|move`` delete or move a whole checkout: a directory removal of that path (rule 4 applies)."""
    op = vals[0] if vals else ""
    if op == "list":
        p._read_only_seg(seg, False)
        return
    _flags, pos, _ = _split_opts(rest[1:], ("--reason",))
    if op == "remove" and pos:
        for w in pos:
            p._target(w, remove=True, is_dir=True)
        return
    if op == "move" and len(pos) >= 2:
        p._target(pos[0], remove=True, is_dir=True)
        p._target(pos[1], is_dir=True)
        return
    p._mark_opaque(seg)


def _git_config(p: _Parser, seg: _Seg, vals: list[str], rw: bool) -> None:
    action: str | None = None
    unset = False
    pos: list[str] = []
    i = 0
    while i < len(vals):
        v = vals[i]
        if v in (
            "--global",
            "--local",
            "--system",
            "--worktree",
            "--includes",
            "--no-includes",
            "--bool",
            "--int",
            "--path",
            "-z",
            "--null",
            "--show-origin",
            "--show-scope",
            "--name-only",
        ):
            i += 1
            continue
        if v in ("--file", "-f", "--blob", "--type", "--default", "--comment"):
            i += 2
            continue
        if v in ("--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--get-color", "--get-colorbool"):
            action = "get"
            i += 1
            continue
        if v in ("--unset", "--unset-all"):
            action, unset = "set", True
            i += 1
            continue
        if v in ("--add", "--replace-all"):
            action = "set"
            i += 1
            continue
        if v in ("--rename-section", "--remove-section"):
            action = "section"
            i += 1
            continue
        if v in ("--edit", "-e"):
            action = "edit"
            i += 1
            continue
        if v.startswith("-"):
            i += 1
            continue
        pos.append(v)
        i += 1
    if pos and pos[0] in ("get", "list"):
        action, pos = "get", pos[1:]
    elif pos and pos[0] in ("set", "unset"):
        action, unset, pos = "set", pos[0] == "unset", pos[1:]
    elif pos and pos[0] in ("rename-section", "remove-section"):
        action, pos = "section", pos[1:]
    elif pos and pos[0] == "edit":
        action, pos = "edit", pos[1:]
    if action == "edit":
        p._mark_opaque(seg)
        return
    if action == "get" or (action is None and len(pos) <= 1):
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    cls = _git_key_class(pos[0]) if pos and action != "section" else "other"
    value = pos[1] if len(pos) > 1 else None
    if cls == "hooks":
        p.res.tamper.add("hooks_path")  # set or unset: core.hooksPath, include.path, includeIf.*.path
        return
    if cls == "alias" and not unset and _git_alias_tamper(value):
        p.res.tamper.add("no_verify")
        return
    if cls == "exec" and not unset and (value is None or not _GIT_SIMPLE_COMMAND_RE.fullmatch(value.strip())):
        # a stored command (a path, a shell form, arguments) that later read-only git runs execute
        p.res.tamper.add("hooks_path")
        return
    p._mark_opaque(seg, scan=True)


def _pathspec_magic(value: str) -> tuple[set[str], str]:
    """Split git pathspec magic: ``:/x`` → ({top}, x); ``:(top,glob)x`` → ({top, glob}, x); ``:!x`` → ({exclude}, x)."""
    if not value.startswith(":") or len(value) < 2:
        return set(), value
    if value.startswith(":("):
        end = value.find(")")
        if end < 0:
            return {"?"}, ""
        magic = {m.strip().split(":", 1)[0].lower() for m in value[2:end].split(",") if m.strip()}
        return magic, value[end + 1 :]
    magic = set()
    j = 1
    short = {"/": "top", "!": "exclude", "^": "exclude"}
    while j < len(value) and value[j] in short:
        magic.add(short[value[j]])
        j += 1
    if j == 1:
        return {"?"}, ""  # ``:<unknown>``: treat as the whole tree
    if j < len(value) and value[j] == ":":
        j += 1
    return magic, value[j:]


def _git_pathspecs(
    p: _Parser,
    words: list[Word],
    *,
    remove: bool = False,
    restore: bool = False,
    is_dir: bool | None = None,
) -> None:
    """Record git pathspecs as write targets.

    Magic is resolved: ``:/`` and ``:(top)`` are relative to the checkout's top level; a wildcard
    pattern (git matches ``*?[`` itself, and ``:(glob)``) becomes its fixed leading directory; an
    ``:(icase)`` pattern becomes its parent directory; exclusions only (``:!x``), ``:(attr:…)`` or
    unknown magic cover the whole scope (the current directory, or the top level with ``top``).
    """
    positives = 0
    whole_top = False
    for w in words:
        if w.dynamic:
            p._target(w)  # the shell decides: opaque and raw-scanned
            positives += 1
            continue
        magic, pattern = _pathspec_magic(w.value)
        top = "top" in magic
        if "exclude" in magic:
            whole_top = whole_top or top
            continue
        positives += 1
        dir_ = is_dir if is_dir is not None else not _has_ext(pattern)
        if magic - {"top", "literal", "glob", "icase"}:
            pattern, dir_ = "", True
        elif "literal" not in magic and any(c in pattern for c in "*?["):
            fixed = pattern[: min(pattern.index(c) for c in "*?[" if c in pattern)]
            pattern, dir_ = (fixed.rsplit("/", 1)[0] if "/" in fixed else ""), True
        elif "icase" in magic:
            pattern, dir_ = posixpath.dirname(pattern.rstrip("/")), True
        rel = pattern.rstrip("/") or "."
        if top:
            p._top_target(rel, remove=remove, restore=restore, is_dir=dir_ or rel == ".")
        else:
            p._target(Word(value=rel, raw=rel), remove=remove, restore=restore, is_dir=dir_ or rel == ".")
    if positives == 0 and words:
        # only exclusions: git applies them to everything under the scope
        if whole_top:
            p._top_target(".", remove=remove, restore=restore, is_dir=True)
        else:
            p._target(Word(value=".", raw="."), remove=remove, restore=restore, is_dir=True)


def _git_checkout(p: _Parser, seg: _Seg, rest: list[Word], cwd: str | None) -> None:
    values_opts = ("-b", "-B", "--orphan", "--conflict", "-t", "--track", "--pathspec-from-file")
    before: list[Word] = []
    after: list[Word] | None = None
    switch = False
    force = False
    from_file = False
    i = 0
    while i < len(rest):
        w = rest[i]
        v = w.value
        if after is not None:
            after.append(w)
            i += 1
            continue
        if v == "--":
            after = []
            i += 1
            continue
        if v in ("-b", "-B", "--orphan"):
            switch = True
            i += 2
            continue
        if v.startswith("--"):
            key, eq, _val = v.partition("=")
            opt = _long_opt(key, _GIT_CHECKOUT_LONG)
            if opt == "--force":
                force = True
            elif opt == "--pathspec-from-file":
                from_file = True
                i += 1 if eq else 2
                continue
            elif opt in values_opts and not eq and opt not in ("--track",):
                i += 2
                continue
            i += 1
            continue
        if v in values_opts:
            i += 2
            continue
        if v.startswith("-") and v != "-":
            if "f" in v[1:]:
                force = True
            i += 1
            continue
        before.append(w)
        i += 1
    if switch:
        p._git_op("switch", cwd)
        p.all_read_only = False
        return
    if from_file:
        p._top_target(".", restore=not before, is_dir=True)
        return
    if after is not None:
        if after:
            _git_pathspecs(p, after, restore=not before)
        elif force:
            p._git_op("reset_hard", cwd)
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    if not before:
        if force:
            p._git_op("reset_hard", cwd)  # `git checkout -f`: discard every local change
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    first = before[0]
    if len(before) == 1:
        if first.value.startswith(":") or first.value == "." or (_has_ext(first.value) and first.value != "-"):
            _git_pathspecs(p, [first], restore=True)
        else:
            p._git_op("checkout_branch", cwd)
            p.all_read_only = False
        return
    no_ref = first.value == "." or first.value.startswith(":") or _has_ext(first.value)
    _git_pathspecs(p, before if no_ref else before[1:], restore=no_ref)


def _git_checkout_index(p: _Parser, seg: _Seg, rest: list[Word], cwd: str | None) -> None:
    """``checkout-index -a`` rewrites every file from the index (a tree-wide op); with paths, those files."""
    flags, pos, values = _split_opts(rest, ("--stage",))
    if any(f.startswith("--prefix") for f in flags) or "--stdin" in flags:
        p._mark_opaque(seg)
        return
    if any(f in ("-a", "--all") or (not f.startswith("--") and "a" in f[1:]) for f in flags):
        p._git_op("reset_hard", cwd)
        p.all_read_only = False
        return
    if not pos:
        p._mark_opaque(seg)
        return
    for w in pos:
        p._target(w, restore=True)


_HANDLERS: Final[Mapping[str, Handler]] = {
    "rm": _h_rm,
    "rmdir": _h_rm,
    "unlink": _h_unlink,
    "touch": _h_touch,
    "mkdir": _h_mkdir,
    "chmod": _h_chmod,
    "chown": _h_chmod,
    "chgrp": _h_chmod,
    "truncate": _h_truncate,
    "dd": _h_dd,
    "tee": _h_tee,
    "sed": _h_sed,
    "gsed": _h_sed,
    "perl": _h_perl,
    "cp": _h_cp,
    "mv": _h_mv,
    "install": _h_install,
    "ln": _h_ln,
    "rsync": _h_rsync,
    "patch": _h_patch,
    "sort": _h_sort,
    "uniq": _h_uniq,
    "tree": _h_tree,
    "less": _h_pager,
    "more": _h_pager,
    "rg": _h_search,
    "fd": _h_search,
    "ag": _h_search,
    "curl": _h_curl,
    "wget": _h_wget,
    "cd": _h_cd,
    "pushd": _h_cd,
    "popd": _h_cd,
    "export": _h_export,
    "declare": _h_export,
    "typeset": _h_export,
    "local": _h_export,
    "readonly": _h_export,
    "unset": _h_unset,
    "kill": _h_kill,
    "pkill": _h_kill,
    "killall": _h_kill,
    "launchctl": _h_launchctl,
    "systemctl": _h_systemctl,
    "eval": _h_eval,
    "source": _h_opaque_code,
    ".": _h_opaque_code,
    "xargs": _h_opaque_code,
    "awk": _h_opaque_code,
    "gawk": _h_opaque_code,
    "nawk": _h_opaque_code,
    "watch": _h_opaque_code,
    "find": _h_find,
    "prettier": _h_prettier,
    "eslint": _h_eslint,
    "biome": _h_biome,
    "ruff": _h_ruff,
    "black": _h_black,
    "gofmt": _h_gofmt,
    "go": _h_go,
    "cargo": _h_cargo,
    "prisma": _h_prisma,
    "supabase": _h_supabase,
    "openapi-generator": _h_openapi,
    "openapi-generator-cli": _h_openapi,
    "graphql-codegen": _h_codegen,
    "tsc": _h_tsc,
    "npm": _h_pm,
    "pnpm": _h_pm,
    "yarn": _h_pm,
    "bun": _h_pm,
    "git": _h_git,
}


# ===========================================================================
# Paths
# ===========================================================================


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def fold(text: str, case_insensitive: bool) -> str:
    """NFC always; case-folded on case-insensitive volumes (§8.2)."""
    out = nfc(text)
    return out.casefold() if case_insensitive else out


def normalize_rel(path: str) -> str:
    """POSIX-normalised repo-relative path (``./`` dropped, no trailing slash, ``.`` for the root)."""
    out = posixpath.normpath(nfc(path).replace("\\", "/")) if path else "."
    return "." if out in ("", "./") else out


def is_under(path: str, root: str, *, case_insensitive: bool = False) -> bool:
    """``path`` equals ``root`` or lies below it (both absolute, normalised)."""
    p, r = fold(path, case_insensitive), fold(root.rstrip("/") or "/", case_insensitive)
    return p == r or p.startswith(r + "/") or r == "/"


def rel_to(path: str, root: str) -> str:
    root = root.rstrip("/")
    if len(path) <= len(root):
        return "."
    return nfc(path[len(root) + 1 :])


class Fs(Protocol):
    """Filesystem access used by :func:`evaluate` (injectable for tests and the server guard)."""

    def realpath(self, path: str) -> str: ...

    def exists(self, path: str) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def inode(self, path: str) -> tuple[int, int, int] | None: ...  # (st_dev, st_ino, st_nlink)

    def read_text(self, path: str, limit: int = 262_144) -> str | None: ...


class OsFs:
    """The real filesystem. Never follows more than the OS does; never raises."""

    def realpath(self, path: str) -> str:
        try:
            return os.path.realpath(path)
        except (OSError, ValueError):
            return posixpath.normpath(path)

    def exists(self, path: str) -> bool:
        try:
            return os.path.lexists(path)
        except (OSError, ValueError):
            return False

    def is_dir(self, path: str) -> bool:
        try:
            return os.path.isdir(path)
        except (OSError, ValueError):
            return False

    def inode(self, path: str) -> tuple[int, int, int] | None:
        try:
            st = os.stat(path)
        except (OSError, ValueError):
            return None
        if not _stat.S_ISREG(st.st_mode):
            return None
        return (st.st_dev, st.st_ino, st.st_nlink)

    def read_text(self, path: str, limit: int = 262_144) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read(limit)
        except (OSError, ValueError):
            return None


# ===========================================================================
# Globs (no regex; linear-time segment matcher with '**')
# ===========================================================================

_GLOB_CHARS: Final = frozenset("*?[")
_SENTINEL: Final = "\x00"
_MAX_BRACE_ALTERNATIVES: Final = 64


def _expand_braces(pattern: str) -> list[str]:
    out = [pattern]
    for _ in range(8):
        nxt: list[str] = []
        changed = False
        for p in out:
            a = p.find("{")
            b = p.find("}", a + 1) if a >= 0 else -1
            if a < 0 or b < 0 or "," not in p[a:b]:
                nxt.append(p)
                continue
            changed = True
            for alt in p[a + 1 : b].split(","):
                nxt.append(p[:a] + alt + p[b + 1 :])
        out = nxt[:_MAX_BRACE_ALTERNATIVES]
        if not changed:
            break
    return out


def _seg_items(seg: str) -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    i = 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            if not items or items[-1][0] != "star":
                items.append(("star", None))
            i += 1
        elif c == "?":
            items.append(("one", None))
            i += 1
        elif c == "[":
            j = seg.find("]", i + 2 if seg[i + 1 : i + 2] in ("!", "^") else i + 1)
            if j < 0:
                items.append(("lit", c))
                i += 1
                continue
            body = seg[i + 1 : j]
            neg = body[:1] in ("!", "^")
            if neg:
                body = body[1:]
            ranges: list[tuple[str, str]] = []
            k = 0
            while k < len(body):
                if k + 2 < len(body) and body[k + 1] == "-":
                    ranges.append((body[k], body[k + 2]))
                    k += 3
                else:
                    ranges.append((body[k], body[k]))
                    k += 1
            items.append(("class", (neg, tuple(ranges))))
            i = j + 1
        else:
            items.append(("lit", c))
            i += 1
    return items


def _item_ok(item: tuple[str, Any], ch: str) -> bool:
    kind, arg = item
    if kind == "lit":
        return bool(arg == ch)
    if kind == "one":
        return ch != _SENTINEL
    neg, ranges = arg
    hit = any(lo <= ch <= hi for lo, hi in ranges)
    return bool(hit != neg)


def _seg_match(items: list[tuple[str, Any]], text: str) -> bool:
    """Wildcard match of one path segment (``*``, ``?``, ``[…]``), greedy with one backtrack point."""
    p = t = 0
    star = -1
    mark = 0
    n = len(items)
    while t < len(text):
        if p < n and items[p][0] == "star":
            star, mark = p, t
            p += 1
        elif p < n and _item_ok(items[p], text[t]):
            p += 1
            t += 1
        elif star != -1:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < n and items[p][0] == "star":
        p += 1
    return p == n


_DSTAR: Final = "**"


class Glob:
    """A compiled path glob: ``**`` spans segments, ``*``/``?``/``[…]`` stay inside one; braces are expanded.

    Leading ``/`` or ``./`` is dropped; a trailing ``/`` means "everything below"; a glob without
    wildcards also matches everything below it (a directory name). Matching never uses a regex.
    """

    __slots__ = ("pattern", "case_insensitive", "_alts")

    def __init__(self, pattern: str, *, case_insensitive: bool = False) -> None:
        self.pattern = pattern
        self.case_insensitive = case_insensitive
        alts: list[list[Any]] = []
        for alt in _expand_braces(fold(pattern, case_insensitive)):
            alt = alt.lstrip("/")
            while alt.startswith("./"):
                alt = alt[2:]
            trailing = alt.endswith("/")
            segs = [s for s in alt.split("/") if s not in ("", ".")]
            if not segs:
                segs = [_DSTAR]
            if trailing:
                segs.append(_DSTAR)
            literal = not any(set(s) & _GLOB_CHARS for s in segs)
            compiled: list[Any] = [_DSTAR if s == _DSTAR else _seg_items(s) for s in segs]
            alts.append(compiled)
            if literal:
                alts.append([*compiled, _DSTAR])
        self._alts = alts

    def _path_segs(self, path: str) -> list[str]:
        return [s for s in fold(path, self.case_insensitive).split("/") if s not in ("", ".")]

    def match(self, path: str) -> bool:
        segs = self._path_segs(path)
        return any(_path_match(alt, segs) for alt in self._alts)

    def contains_dir(self, directory: str) -> bool:
        """Every file directly inside ``directory`` would match (the directory lies inside the glob's area)."""
        segs = [*self._path_segs(directory), _SENTINEL]
        return any(_path_match(alt, segs) for alt in self._alts)

    def overlaps_dir(self, directory: str) -> bool:
        """Some path at or below ``directory`` could match."""
        segs = self._path_segs(directory)
        return any(_overlap(alt, segs) for alt in self._alts)

    def static_prefix(self) -> str:
        first = self._alts[0] if self._alts else []
        out: list[str] = []
        for item in first:
            if item == _DSTAR or any(k != "lit" for k, _ in item):
                break
            out.append("".join(ch for _, ch in item))
        return "/".join(out)


def _path_match(items: list[Any], segs: list[str]) -> bool:
    p = t = 0
    star = -1
    mark = 0
    n = len(items)
    while t < len(segs):
        if p < n and items[p] == _DSTAR:
            star, mark = p, t
            p += 1
        elif p < n and _seg_match(items[p], segs[t]):
            p += 1
            t += 1
        elif star != -1:
            p = star + 1
            mark += 1
            t = mark
        else:
            return False
    while p < n and items[p] == _DSTAR:
        p += 1
    return p == n


def _overlap(items: list[Any], segs: list[str]) -> bool:
    for k, seg in enumerate(segs):
        if k >= len(items):
            return False
        item = items[k]
        if item == _DSTAR:
            return True
        if not _seg_match(item, seg):
            return False
    return True


_GLOB_CACHE: dict[tuple[str, bool], Glob] = {}


def compile_glob(pattern: str, case_insensitive: bool = False) -> Glob:
    key = (pattern, case_insensitive)
    g = _GLOB_CACHE.get(key)
    if g is None:
        if len(_GLOB_CACHE) > 4096:
            _GLOB_CACHE.clear()
        g = _GLOB_CACHE[key] = Glob(pattern, case_insensitive=case_insensitive)
    return g


def glob_match(pattern: str, path: str, *, case_insensitive: bool = False) -> bool:
    """Match a repo-relative ``path`` against a zone/commons/ignore glob (NFC; case-folded when asked)."""
    return compile_glob(pattern, case_insensitive).match(path)


# ===========================================================================
# Command-pattern trie (D38)
# ===========================================================================


class _TrieNode:
    __slots__ = ("children", "star", "values")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.star: _TrieNode | None = None
        self.values: set[str] = set()


class CommandTrie:
    """Argv-prefix token patterns compiled into a trie. ``*`` matches exactly one token; a final ``*`` any tail.

    Matching is linear in ``len(argv)`` times the number of live branches; no regex ever runs.
    Patterns are validated with :func:`remembra.crew.schemas.validate_command_pattern`.
    """

    def __init__(self) -> None:
        self._root = _TrieNode()
        self.size = 0

    def add(self, pattern: str, value: str) -> None:
        errors = S.validate_command_pattern(pattern)
        if errors:
            raise ValueError(f"invalid command pattern {pattern!r}: {'; '.join(errors)}")
        tokens = pattern.split(" ")
        if tokens[-1] == "*":
            tokens = tokens[:-1]
        node = self._root
        for tok in tokens:
            if tok == "*":
                if node.star is None:
                    node.star = _TrieNode()
                node = node.star
            else:
                node = node.children.setdefault(tok, _TrieNode())
        node.values.add(value)
        self.size += 1

    def match(self, argv: Sequence[str]) -> set[str]:
        out: set[str] = set()
        live = [self._root]
        for tok in argv:
            nxt: list[_TrieNode] = []
            for node in live:
                out |= node.values
                child = node.children.get(tok)
                if child is not None:
                    nxt.append(child)
                if node.star is not None:
                    nxt.append(node.star)
            live = nxt
            if not live:
                return out
        for node in live:
            out |= node.values
        return out


# ===========================================================================
# Snapshot index: zones, commons, ignore, claims, checkouts
# ===========================================================================

LIVE_HOLD_STATES: Final = ("active", "offered")
BLOCKING_STATES: Final = ("active", "offered", "reserved")
FENCE_MARGIN_S: Final = 60
OFFLINE_SNAPSHOT_MAX_AGE_S: Final = 30 * 60
OFFLINE_MAX_SKEW_S: Final = 300


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CheckoutGroup:
    """One checkout on this host (``toplevel``) with every session that works in it."""

    toplevel: str
    worktree_id: str
    git_common_dir: str
    case_insensitive: bool
    session_ids: tuple[str, ...]
    default_branch: str | None


class ZoneIndex:
    """Compiled zones (with nesting), commons, ignore, the command trie and the MCP zone rules."""

    def __init__(self, zones: Sequence[Mapping[str, Any]], commons: Sequence[Mapping[str, Any]], ignore: Sequence[str]) -> None:
        self.zones = [z for z in zones if not z.get("archived_at")]
        self.by_id: dict[str, Mapping[str, Any]] = {str(z["id"]): z for z in self.zones}
        self.builtin = [z for z in self.zones if z.get("builtin")]
        self.user_zones = [z for z in self.zones if not z.get("builtin")]
        self.commons = list(commons)
        self.ignore = list(ignore)
        self.trie = CommandTrie()
        self.bad_patterns: list[str] = []
        for z in self.user_zones:
            for pat in z.get("command_patterns") or ():
                try:
                    self.trie.add(str(pat), str(z["id"]))
                except ValueError:
                    self.bad_patterns.append(str(pat))
        self.mcp_rules: list[tuple[str, Mapping[str, Any]]] = [
            (str(z["slug"]), r) for z in self.user_zones for r in (z.get("mcp_tools") or ()) if isinstance(r, Mapping)
        ]

    def ancestors(self, zone_id: str) -> list[Mapping[str, Any]]:
        out: list[Mapping[str, Any]] = []
        seen = {zone_id}
        cur = self.by_id.get(zone_id)
        while cur is not None and cur.get("parent_id") and cur["parent_id"] not in seen:
            seen.add(str(cur["parent_id"]))
            cur = self.by_id.get(str(cur["parent_id"]))
            if cur is not None:
                out.append(cur)
        return out

    def _in_zone(self, z: Mapping[str, Any], rel: str, ci: bool) -> bool:
        if not any(compile_glob(str(g), ci).match(rel) for g in z.get("include_globs") or ()):
            return False
        return not any(compile_glob(str(g), ci).match(rel) for g in z.get("exclude_globs") or ())

    def _with_ancestors(self, zones: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        out: dict[str, Mapping[str, Any]] = {}
        for z in zones:
            out[str(z["id"])] = z
            for a in self.ancestors(str(z["id"])):
                out.setdefault(str(a["id"]), a)
        return list(out.values())

    def match(self, rel: str, ci: bool) -> list[Mapping[str, Any]]:
        """User zones containing the file ``rel`` plus their ancestors (holding a parent covers children)."""
        return self._with_ancestors(z for z in self.user_zones if self._in_zone(z, rel, ci))

    def containing_dir(self, rel: str, ci: bool) -> list[Mapping[str, Any]]:
        hits = []
        for z in self.user_zones:
            if any(compile_glob(str(g), ci).contains_dir(rel) for g in z.get("include_globs") or ()):
                hits.append(z)
        return self._with_ancestors(hits)

    def overlapping_dir(self, rel: str, ci: bool) -> list[Mapping[str, Any]]:
        if rel == ".":
            return list(self.user_zones)
        return [
            z for z in self.user_zones if any(compile_glob(str(g), ci).overlaps_dir(rel) for g in z.get("include_globs") or ())
        ]

    def commons_entry(self, rel: str, ci: bool) -> Mapping[str, Any] | None:
        for entry in self.commons:
            if compile_glob(str(entry.get("glob", "")), ci).match(rel):
                return entry
        return None

    def ignored(self, rel: str, ci: bool) -> bool:
        return any(compile_glob(str(g), ci).match(rel) for g in self.ignore)

    def zones_with_service(self, service: str) -> list[Mapping[str, Any]]:
        return [z for z in self.user_zones if service in (z.get("services") or ())]

    def policy_globs(self) -> tuple[list[str], list[str]]:
        """(repo-relative, home-relative) crew-policy globs: the built-in zone's plus the contract defaults (D28)."""
        repo: list[str] = []
        home: list[str] = []
        for g in [*S.CREW_POLICY_GLOBS, *(str(x) for z in self.builtin for x in z.get("include_globs") or ())]:
            if g.startswith("~/"):
                if g[2:] not in home:
                    home.append(g[2:])
            elif g not in repo:
                repo.append(g)
        return repo, home


class SnapshotIndex:
    """Read-only lookups over one local snapshot (``LocalSnapshot``; the server snapshot works without checkouts)."""

    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.raw = snapshot
        self.crew: Mapping[str, Any] = snapshot.get("crew") or {}
        self.settings: Mapping[str, Any] = snapshot.get("settings") or {}
        self.sessions: dict[str, Mapping[str, Any]] = {str(s["id"]): s for s in snapshot.get("sessions") or ()}
        self.tasks: dict[str, Mapping[str, Any]] = {str(t["id"]): t for t in snapshot.get("tasks") or ()}
        self.claims: list[Mapping[str, Any]] = [c for c in snapshot.get("claims") or () if c.get("state") in S.LIVE_CLAIM_STATES]
        self.offers: list[Mapping[str, Any]] = list(snapshot.get("offers") or ())
        self.footprints: list[Mapping[str, Any]] = list(snapshot.get("footprints") or ())
        self.zones = ZoneIndex(snapshot.get("zones") or (), snapshot.get("commons") or (), snapshot.get("ignore") or ())
        self.host_id = snapshot.get("host_id")
        self.synced_at = parse_ts(snapshot.get("synced_at")) or parse_ts(snapshot.get("server_time"))
        self.skew_s = float(snapshot.get("skew_s") or 0.0)
        groups: dict[str, dict[str, Any]] = {}
        for co in snapshot.get("checkouts") or ():
            top = posixpath.normpath(nfc(str(co.get("toplevel", ""))))
            g = groups.setdefault(
                top,
                {
                    "worktree_id": str(co.get("worktree_id", "")),
                    "git_common_dir": posixpath.normpath(nfc(str(co.get("git_common_dir") or top + "/.git"))),
                    "ci": bool(co.get("case_insensitive")),
                    "sessions": [],
                    "default_branch": co.get("default_branch"),
                },
            )
            if co.get("session_id"):
                g["sessions"].append(str(co["session_id"]))
            g["ci"] = g["ci"] or bool(co.get("case_insensitive"))
        self.checkouts = [
            CheckoutGroup(top, g["worktree_id"], g["git_common_dir"], g["ci"], tuple(g["sessions"]), g["default_branch"])
            for top, g in groups.items()
        ]

    # -- sessions --------------------------------------------------------
    def callsign(self, session_id: str | None) -> str:
        s = self.sessions.get(session_id or "")
        return str(s.get("callsign")) if s and s.get("callsign") else "another session"

    def parent_of(self, session_id: str | None) -> str | None:
        """The session a sub-agent session reports to (owner decision: a sub-agent is its own session)."""
        s = self.sessions.get(session_id or "")
        parent = s.get("parent_session_id") if s else None
        return str(parent) if parent else None

    def holder_label(self, session_id: str | None) -> str:
        """``cc-2``, or ``cc-2 (sub-agent of cc-1)``: the claim is the sub-agent's, its parent answers for it."""
        who = self.callsign(session_id)
        parent = self.parent_of(session_id)
        return f"{who} (sub-agent of {self.callsign(parent)})" if parent else who

    def is_live(self, session_id: str) -> bool:
        s = self.sessions.get(session_id)
        return s is not None and s.get("state") in S.LIVE_PRESENCE_STATES

    def is_live_or_reserved(self, session_id: str) -> bool:
        if self.is_live(session_id):
            return True
        return any(c.get("holder_session_id") == session_id and c.get("state") == "reserved" for c in self.claims)

    def task_label(self, task_id: str | None) -> str | None:
        t = self.tasks.get(task_id or "")
        return f"T-{t['number']}" if t and t.get("number") is not None else None

    # -- checkouts -------------------------------------------------------
    def locate(self, abs_path: str) -> CheckoutGroup | None:
        best: CheckoutGroup | None = None
        for co in self.checkouts:
            if is_under(abs_path, co.toplevel, case_insensitive=co.case_insensitive):
                if best is None or len(co.toplevel) > len(best.toplevel):
                    best = co
        return best

    def own_checkouts(self, caller: str) -> list[CheckoutGroup]:
        return [co for co in self.checkouts if caller in co.session_ids]


# ===========================================================================
# Evaluation
# ===========================================================================

AUTO_CLAIM_RESULTS: Final = S.AUTO_CLAIM_RESULTS


@dataclass(frozen=True)
class ClaimRequest:
    """An auto-claim the gate asks crewd (or the server) for (row 17, D11)."""

    zone_id: str | None
    zone_slug: str | None
    mode: str
    path_glob: str | None = None  # file-level claim (undeclared_policy = file_claim)


@dataclass(frozen=True)
class ClaimResult:
    result: str  # granted | conflict | cap | timeout | rate_limited
    winner_session_id: str | None = None
    claim_id: str | None = None


ClaimFn = Callable[[ClaimRequest], "ClaimResult | str"]


@dataclass
class Target:
    """One thing a tool call touches: a file, a directory, a zone, a service or the command itself."""

    kind: str  # path | dir | zone | service | command
    op: str = "write"  # write | remove | create | edit | tree | command
    abs_path: str | None = None
    rel: str | None = None
    display: str | None = None  # repo-relative path shown in texts (never absolute)
    checkout: CheckoutGroup | None = None
    zone_ids: tuple[str, ...] = ()
    service: str | None = None
    overlap: bool = True  # dir targets: also check zones below the directory
    content: str | None = None  # Write content (settings surgical protection)
    edits: tuple[tuple[str, str, bool], ...] = ()  # (old, new, replace_all) of Edit/MultiEdit/MCP edit_file


@dataclass
class _Unit:
    target: Target
    facts: dict[str, Any]
    info: dict[str, Any]  # reason data: zone, claim, holder, service, …


@dataclass
class Verdict:
    """The gate's answer for one tool call."""

    rule: int
    decision: str  # allow | deny | ask | warn
    variant: str = ""
    effects: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    target: Target | None = None
    claims: list[tuple[ClaimRequest, ClaimResult]] = field(default_factory=list)
    post_tool_check: bool = False
    tamper_kinds: tuple[str, ...] = ()
    zone_slug: str | None = None
    holder_session_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "decision": self.decision,
            "variant": self.variant,
            "effects": list(self.effects),
            "facts": dict(self.facts),
            "reason": self.reason,
            "post_tool_check": self.post_tool_check,
            "tamper_kinds": list(self.tamper_kinds),
            "zone": self.zone_slug,
            "holder_session_id": self.holder_session_id,
            "claims": [
                {"zone_id": r.zone_id, "zone": r.zone_slug, "mode": r.mode, "path_glob": r.path_glob, "result": res.result}
                for r, res in self.claims
            ],
        }

    def hook_stdout(self) -> str:
        """Claude Code PreToolUse stdout (§8.2): empty on allow/warn; one JSON line on deny/ask."""
        if self.decision == "deny" and self.reason:
            return S.hook_pretool_deny(self.reason)
        if self.decision == "ask" and self.reason:
            return S.hook_pretool_ask(self.reason)
        return S.hook_allow()


_EDIT_TOOLS: Final = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_MARKER_HOME_GLOBS: Final = (".claude/settings*.json",)
_MARKER_REPO_GLOBS: Final = (
    ".claude/settings*.json",
    ".husky/**",
    "lefthook*",
    ".lefthook*",
    ".config/lefthook*",
    "lefthook-local.yml",
)
_MARKER_CMD_RE: Final = re.compile(r'([^"\n]*?' + re.escape(S.CREW_HOOK_MARKER) + r")")
_SEVERITY: Final = {"deny": 3, "ask": 2, "warn": 1, "allow": 0}


def marker_entries(text: str) -> set[str]:
    """Crew hook entries in a settings/Husky/lefthook file: each command text ending in the marker."""
    return {m.group(1).strip() for m in _MARKER_CMD_RE.finditer(text or "")}


class _Ctx:
    def __init__(
        self,
        idx: SnapshotIndex,
        caller: str,
        now: datetime,
        fs: Fs,
        home: str,
        mode: str,
        *,
        server_reachable: bool,
        server_outage: bool,
        unconfirmed_zone_ids: frozenset[str],
        github_repos: frozenset[str],
    ) -> None:
        self.idx = idx
        self.caller = caller
        self.now = now
        self.fs = fs
        self.home = posixpath.normpath(home)
        self.mode = mode
        self.server_reachable = server_reachable
        self.server_outage = server_outage
        self.unconfirmed_zone_ids = unconfirmed_zone_ids
        self.github_repos = github_repos
        self.caller_session = idx.sessions.get(caller) or {}
        self.effects: list[str] = []
        repo_globs, home_globs = idx.zones.policy_globs()
        self.policy_repo_globs = repo_globs
        self.policy_home_globs = home_globs

    # -- claims visible to the decision (offline policy D12) --------------
    def counts(self, claim: Mapping[str, Any], zone: Mapping[str, Any] | None) -> bool:
        if self.server_reachable:
            return True
        holder = self.idx.sessions.get(str(claim.get("holder_session_id") or ""))
        if holder is not None and holder.get("host_id") and holder.get("host_id") == self.idx.host_id:
            return True  # local arbiter: same-host holders fail closed regardless of snapshot age
        if zone is not None and (
            zone.get("fail_closed") or zone.get("slug") in (self.idx.settings.get("fail_closed_zones") or ())
        ):
            return True
        synced = self.idx.synced_at
        fresh = synced is not None and (self.now - synced).total_seconds() < OFFLINE_SNAPSHOT_MAX_AGE_S
        if fresh and abs(self.idx.skew_s) <= OFFLINE_MAX_SKEW_S:
            return True
        if "offline_stale_allow" not in self.effects:
            self.effects.append("offline_stale_allow")
        return False

    def is_mine(self, claim: Mapping[str, Any]) -> bool:
        return claim.get("holder_kind", "session") == "session" and claim.get("holder_session_id") == self.caller

    def zone_claims(self, zone_ids: Iterable[str]) -> list[Mapping[str, Any]]:
        ids = set(zone_ids)
        return [c for c in self.idx.claims if c.get("zone_id") in ids]

    def lease_ok(self, claim: Mapping[str, Any]) -> bool:
        if self.server_outage:
            return True
        if claim.get("fenced"):
            return False
        exp = parse_ts(claim.get("lease_expires_at"))
        if exp is None:
            return True
        return self.now < exp - timedelta(seconds=FENCE_MARGIN_S)


def _now_dt(now: datetime | str | None) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=UTC)
    parsed = parse_ts(now) if now else None
    return parsed or datetime.now(UTC)


def mode_for(snapshot: Mapping[str, Any], caller: str) -> str | None:
    """enforce / observe for this caller, or ``None`` when crew enforcement is off (the gate exits)."""
    settings = snapshot.get("settings") or {}
    level = settings.get("enforcement") or (snapshot.get("crew") or {}).get("enforcement") or "enforce"
    if level == "off":
        return None
    if level == "observe":
        return "observe"
    for s in snapshot.get("sessions") or ():
        if s.get("id") == caller and s.get("adapter_enforcement") == "advisory":
            return "observe"
    return "enforce"


def evaluate(
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    caller: str,
    cwd: str,
    home: str,
    now: datetime | str | None = None,
    mode: str | None = None,
    permission_mode: str = "default",
    fs: Fs | None = None,
    claim: ClaimFn | None = None,
    server_reachable: bool = True,
    server_outage: bool = False,
    unconfirmed_zone_ids: Iterable[str] = (),
    github_repos: Iterable[str] = (),
    human: str = "the owner",
    tz: tzinfo | None = None,
    protected_pids: Iterable[int] = (),
) -> Verdict:
    """Decide one tool call (§5.2, §8.2).

    ``snapshot`` is the local snapshot (``LocalSnapshot``). ``now`` is the server clock (the gate
    adds the measured skew). ``claim`` is called only when first-match reaches row 17 (or row 19
    with ``undeclared_policy = file_claim``); it returns a result in ``AUTO_CLAIM_RESULTS`` (or a
    :class:`ClaimResult`). Without a callback an auto-claim counts as ``timeout`` (D11: that one
    write is allowed and spooled ``unconfirmed``; the next is denied until confirmed).
    ``protected_pids`` are crewd's pid(s): a ``kill`` naming one of them (or ``-1``) is a
    ``crewd_kill`` tamper (row 2), like ``pkill -f remembra-crewd``.

    Rule 0 (fast exit before the table): read-only Bash, read-like MCP tools, paths outside every
    known checkout that are not crew policy, and opaque Bash with nothing else to check.
    """
    if not isinstance(tool_input, Mapping):
        tool_input = {}
    parsed: BashParse | None = None
    if tool_name == "Bash":
        parsed = parse_bash_full(str(tool_input.get("command") or ""))
        if parsed.read_only:
            return Verdict(0, "allow", "read_only")  # no snapshot read at all (§8.2 read-only fast exit)
        guarded = {int(x) for x in protected_pids if isinstance(x, int) and x > 1}
        if guarded and parsed.kill_pids and (parsed.kill_pids & guarded or -1 in parsed.kill_pids):
            parsed.tamper.add("crewd_kill")  # `kill -9 <crewd pid>` (or every process) stops crewd (§5.2 row 2)
    elif _fast_read(tool_name, tool_input):
        return Verdict(0, "allow", "read_only")
    idx = SnapshotIndex(snapshot)
    fs = fs or OsFs()
    now_dt = _now_dt(now)
    eff_mode = mode or mode_for(snapshot, caller)
    if eff_mode is None:
        return Verdict(0, "allow", "crew_off")
    if eff_mode not in ("enforce", "observe"):
        raise ValueError(f"mode must be enforce or observe, got {eff_mode!r}")
    ctx = _Ctx(
        idx,
        caller,
        now_dt,
        fs,
        home,
        eff_mode,
        server_reachable=server_reachable,
        server_outage=server_outage,
        unconfirmed_zone_ids=frozenset(unconfirmed_zone_ids),
        github_repos=frozenset(github_repos),
    )
    settings = idx.settings
    interactive = bool(settings.get("interactive_override"))

    targets, command_facts, command_info, fast = _extract(tool_name, tool_input, cwd, ctx, parsed)
    if fast is not None:
        return fast

    units: list[_Unit] = []
    paused = ctx.caller_session.get("state") == "paused"
    for t in _expand_hardlinks(targets, ctx):
        facts, info = _target_facts(t, ctx)
        if info.get("outside") and not facts:
            continue  # rule 0: outside every checkout and not crew policy
        if paused:
            facts["session_paused"] = True
        units.append(_Unit(t, facts, info))
    command_unit = any(command_facts.get(p) for p in S.GUARD_PREDICATES)
    if paused and not units and not targets and (command_info.get("bash") or command_info.get("post_tool_check")):
        command_unit = True  # a paused session may still read, but nothing that writes
    if command_unit:
        cf = dict(command_facts)
        if paused:
            cf["session_paused"] = True
        units.append(_Unit(Target("command", op="command"), cf, {**command_info, "target": None}))

    post_check = bool(command_info.get("post_tool_check"))
    if not units:
        is_bash = bool(command_info.get("bash"))
        variant = "post_tool_check" if post_check or (is_bash and not targets) else "outside_checkouts"
        return Verdict(0, "allow", variant, tuple(ctx.effects), post_tool_check=post_check or is_bash)

    # Pass 1: decide every unit that does not need an auto-claim.
    decided: list[tuple[_Unit, S.GuardOutcome]] = []
    need_claim: list[_Unit] = []
    for u in units:
        first = _first_rule(u.facts)
        wants_claim = first == 17 and u.facts.get("auto_claim_enabled", True)
        if wants_claim or (first == 19 and u.facts.get("undeclared_policy") == "file_claim" and u.info.get("claim_request")):
            need_claim.append(u)
            continue
        out = S.guard_decide(u.facts, eff_mode, interactive_override=interactive, permission_mode=permission_mode)
        decided.append((u, out))
    worst = _worst(decided)
    claims: list[tuple[ClaimRequest, ClaimResult]] = []
    if need_claim and (worst is None or worst[1].decision not in ("deny", "ask")):
        results: dict[tuple[str | None, str | None], ClaimResult] = {}
        for u in need_claim:
            req: ClaimRequest = u.info["claim_request"]
            key = (req.zone_id, req.path_glob)
            if key not in results:
                results[key] = _call_claim(claim, req)
                claims.append((req, results[key]))
            res = results[key]
            u.facts["auto_claim_result"] = res.result
            if res.winner_session_id:
                u.info["winner"] = res.winner_session_id
            out = S.guard_decide(u.facts, eff_mode, interactive_override=interactive, permission_mode=permission_mode)
            decided.append((u, out))
        worst = _worst(decided)
    # otherwise a deny/ask elsewhere wins and no claim is attempted for a write that will not happen
    assert worst is not None
    unit, out = worst
    effects = list(
        dict.fromkeys([*out.effects, *ctx.effects, *(e for _, o in decided for e in o.effects if e.startswith("collision:"))])
    )
    verdict = Verdict(
        rule=out.rule,
        decision=out.decision,
        variant=out.variant,
        effects=tuple(effects),
        facts=dict(unit.facts),
        target=unit.target,
        claims=claims,
        post_tool_check=post_check,
        tamper_kinds=tuple(sorted(unit.info.get("tamper_kinds", ()))),
        zone_slug=(unit.info.get("zone") or {}).get("slug"),
        holder_session_id=unit.info.get("holder_session_id"),
    )
    if verdict.decision in ("deny", "ask", "warn"):
        verdict.reason = render_deny_reason(verdict, unit.info, idx, now_dt, caller=caller, human=human, tz=tz)
    return verdict


def _fast_read(tool_name: str, tool_input: Mapping[str, Any]) -> bool:
    """Read-like MCP tools and built-in tools the gate does not gate exit before the snapshot is read."""
    if tool_name.startswith("mcp__"):
        return S.classify_mcp_tool(tool_name, tool_input).kind == "read"
    return tool_name not in _EDIT_TOOLS


def _call_claim(fn: ClaimFn | None, req: ClaimRequest) -> ClaimResult:
    if fn is None:
        return ClaimResult("timeout")
    try:
        res = fn(req)
    except Exception:  # noqa: BLE001 - a failing claim path is a timeout (D11), never a crash in the gate
        return ClaimResult("timeout")
    if isinstance(res, str):
        res = ClaimResult(res)
    if res.result not in AUTO_CLAIM_RESULTS:
        return ClaimResult("timeout")
    return res


def _first_rule(facts: Mapping[str, Any]) -> int:
    for rule in S.GUARD_RULES:
        if any(bool(facts.get(p)) for p in rule.any_of):
            return rule.number
    return 19


def _worst(decided: Sequence[tuple[_Unit, S.GuardOutcome]]) -> tuple[_Unit, S.GuardOutcome] | None:
    best: tuple[_Unit, S.GuardOutcome] | None = None
    for u, out in decided:
        if best is None:
            best = (u, out)
            continue
        a, b = _SEVERITY[out.decision], _SEVERITY[best[1].decision]
        if a > b or (a == b and out.rule < best[1].rule):
            best = (u, out)
    return best


# -- target extraction ------------------------------------------------------


def _abs(cwd: str, path: str, home: str) -> str:
    p = nfc(path)
    if p == "~":
        p = home
    elif p.startswith("~/"):
        p = posixpath.join(home, p[2:])
    elif not p.startswith("/"):
        p = posixpath.join(cwd, p)
    return posixpath.normpath(p)


def _extract(
    tool_name: str, tool_input: Mapping[str, Any], cwd: str, ctx: _Ctx, parsed: BashParse | None = None
) -> tuple[list[Target], dict[str, Any], dict[str, Any], Verdict | None]:
    targets: list[Target] = []
    cfacts: dict[str, Any] = {}
    cinfo: dict[str, Any] = {}
    if tool_name in _EDIT_TOOLS:
        raw = tool_input.get(_EDIT_TOOLS[tool_name])
        if not isinstance(raw, str) or not raw:
            return [], {}, {}, Verdict(0, "allow", "outside_checkouts")
        edits: list[tuple[str, str, bool]] = []
        if tool_name == "Edit":
            edits.extend(_edit_list([tool_input], "old_string", "new_string"))
        if tool_name == "MultiEdit":
            edits.extend(_edit_list(tool_input.get("edits") or (), "old_string", "new_string"))
        content = tool_input.get("content") if tool_name == "Write" and isinstance(tool_input.get("content"), str) else None
        op = "write" if tool_name == "Write" else "edit"
        targets.append(Target("path", op=op, abs_path=_abs(cwd, raw, ctx.home), content=content, edits=tuple(edits)))
        return targets, cfacts, cinfo, None
    if tool_name == "Bash":
        return _extract_bash(parsed or parse_bash_full(str(tool_input.get("command") or "")), cwd, ctx)
    if tool_name.startswith("mcp__"):
        return _extract_mcp(tool_name, tool_input, cwd, ctx)
    return [], {}, {}, Verdict(0, "allow", "read_only")


def _extract_bash(parsed: BashParse, cwd: str, ctx: _Ctx) -> tuple[list[Target], dict[str, Any], dict[str, Any], Verdict | None]:
    if parsed.read_only:
        return [], {}, {}, Verdict(0, "allow", "read_only")
    cfacts: dict[str, Any] = {}
    cinfo: dict[str, Any] = {"bash": True, "post_tool_check": parsed.opaque or bool(parsed.git_tree_op) or parsed.tree_writer}
    targets: list[Target] = []
    if parsed.tamper:
        cfacts["tamper_command"] = True
        cinfo["tamper_kinds"] = set(parsed.tamper)
    for w in sorted(parsed.writes):
        op = "remove" if w in parsed.removes else ("restore" if w in parsed.restores else "write")
        kind = "dir" if w in parsed.dirs else "path"
        # restoring from the index or HEAD only undoes this checkout's own uncommitted changes: it is
        # checked against the zone that contains it and against other sessions' dirty files, not
        # against every zone below it
        targets.append(Target(kind, op=op, abs_path=_abs(cwd, w, ctx.home), overlap=op != "restore"))
    # git `:/` and `:(top)` pathspecs name paths from the top level of the checkout the command runs in
    for seg_cwd, rel, remove, restore, is_dir in parsed.top_writes:
        base = _abs(cwd, seg_cwd, ctx.home)
        co = ctx.idx.locate(ctx.fs.realpath(base))
        top = co.toplevel if co is not None else base
        op = "remove" if remove else ("restore" if restore else "write")
        a = posixpath.normpath(posixpath.join(top, rel))
        targets.append(Target("dir" if is_dir else "path", op=op, abs_path=a, overlap=op != "restore"))
    # tree writers (row 13) and their scopes
    if parsed.tree_writer:
        blockers: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for sc in sorted(parsed.tree_scope):
            a = _abs(cwd, sc, ctx.home)
            co = ctx.idx.locate(a)
            targets.append(Target("dir", op="tree", abs_path=a, overlap=False))
            if co is None:
                continue
            rel = rel_to(a, co.toplevel)
            for z in ctx.idx.zones.overlapping_dir(rel, co.case_insensitive):
                for c in ctx.zone_claims([str(z["id"])]):
                    if (
                        c.get("mode") == "exclusive"
                        and c.get("state") in BLOCKING_STATES
                        and not ctx.is_mine(c)
                        and ctx.counts(c, z)
                    ):
                        blockers.append((z, c))
        if blockers:
            cfacts["tree_writer_other_exclusive"] = True
            cinfo.setdefault("zone", blockers[0][0])
            cinfo.setdefault("claim", blockers[0][1])
            cinfo.setdefault("holder_session_id", blockers[0][1].get("holder_session_id"))
            cinfo["reason_kind"] = "tree_writer"
    # tree-wide git ops (row 15)
    if parsed.git_tree_op:
        a = _abs(cwd, parsed.git_tree_op_cwd or ".", ctx.home)
        co = ctx.idx.locate(a)
        if co is not None:
            if ctx.caller not in co.session_ids and any(ctx.idx.is_live_or_reserved(s) for s in co.session_ids):
                cfacts["foreign_checkout"] = True
                cinfo["foreign_session"] = next(s for s in co.session_ids if ctx.idx.is_live_or_reserved(s))
            others = [s for s in co.session_ids if s != ctx.caller and ctx.idx.is_live(s)]
            if others:
                cfacts["tree_git_op_other_live_same_checkout"] = True
                cinfo["holder_session_id"] = others[0]
                cinfo["git_op"] = parsed.git_tree_op
                cinfo.setdefault("reason_kind", "tree_git_op")
    # zone command patterns (D38) → zones and services (row 14)
    for _seg_cwd, argv in parsed.argvs:
        zone_ids = ctx.idx.zones.trie.match(argv)
        for zid in sorted(zone_ids):
            pz = ctx.idx.zones.by_id.get(zid)
            if pz is None:
                continue
            services = list(pz.get("services") or ())
            for svc in services:
                targets.append(Target("service", op="command", service=str(svc), zone_ids=(zid,)))
            if not services:
                targets.append(Target("zone", op="command", zone_ids=(zid,)))
    return targets, cfacts, cinfo, None


def _extract_mcp(
    tool_name: str, tool_input: Mapping[str, Any], cwd: str, ctx: _Ctx
) -> tuple[list[Target], dict[str, Any], dict[str, Any], Verdict | None]:
    cls = S.classify_mcp_tool(tool_name, tool_input, ctx.idx.zones.mcp_rules)
    if cls.kind == "read":
        return [], {}, {}, Verdict(0, "allow", "read_only")
    targets: list[Target] = []
    cinfo: dict[str, Any] = {}
    if cls.kind == "services":
        zone_ids: tuple[str, ...] = ()
        if cls.zone_slug:
            zone_ids = tuple(str(z["id"]) for z in ctx.idx.zones.user_zones if z.get("slug") == cls.zone_slug)
        for svc in cls.services:
            targets.append(Target("service", op="command", service=svc, zone_ids=zone_ids))
    elif cls.kind == "zone":
        zone_ids = tuple(str(z["id"]) for z in ctx.idx.zones.user_zones if z.get("slug") == cls.zone_slug)
        if zone_ids:
            targets.append(Target("zone", op="command", zone_ids=zone_ids))
    elif cls.kind == "paths":
        if cls.github_repo is not None:
            if cls.github_repo not in ctx.github_repos:
                return [], {}, {}, Verdict(0, "allow", "outside_checkouts")
            own = ctx.idx.own_checkouts(ctx.caller)
            base = own[0].toplevel if own else cwd
            for p in cls.paths:
                targets.append(Target("path", op="write", abs_path=_abs(base, p.lstrip("/"), ctx.home)))
        else:
            edits = tuple(_edit_list(tool_input.get("edits") or (), "oldText", "newText"))
            content = tool_input.get("content") if isinstance(tool_input.get("content"), str) else None
            for p in cls.paths:
                targets.append(Target("path", op="write", abs_path=_abs(cwd, p, ctx.home), content=content, edits=edits))
    else:
        cinfo["post_tool_check"] = True
    return targets, {}, cinfo, None


def _expand_hardlinks(targets: list[Target], ctx: _Ctx) -> list[Target]:
    """A file with ``st_nlink > 1`` is also checked as every path crewd knows for that inode (from footprints)."""
    out = list(targets)
    for t in targets:
        if t.kind != "path" or t.abs_path is None:
            continue
        ino = ctx.fs.inode(ctx.fs.realpath(t.abs_path))
        if ino is None or ino[2] <= 1:
            continue
        found = False
        for fp in ctx.idx.footprints:
            for co in ctx.idx.checkouts:
                cand = posixpath.join(co.toplevel, str(fp.get("path", "")))
                other = ctx.fs.inode(cand)
                if other is not None and other[:2] == ino[:2] and cand != t.abs_path:
                    out.append(Target("path", op=t.op, abs_path=cand))
                    found = True
        if not found and "hardlink_unknown" not in ctx.effects:
            ctx.effects.append("hardlink_unknown")
    return out


# -- predicates per target ----------------------------------------------------


def _outside_display(abs_path: str, ctx: _Ctx) -> str | None:
    """A host-neutral label for a path outside every checkout (never the absolute home path)."""
    if is_under(abs_path, ctx.home, case_insensitive=True):
        return "~/" + rel_to(abs_path, ctx.home)
    for c in ctx.idx.checkouts:
        if is_under(abs_path, c.git_common_dir, case_insensitive=True):
            return ".git/" + rel_to(abs_path, c.git_common_dir)
    return None


# Git config files: a direct write can set core.hooksPath or an include (§5.2 row 2), so they are
# crew policy like the hooks directory. Home-relative, then relative to the git common dir and the top level.
_GIT_CONFIG_HOME_FILES: Final = (".gitconfig", ".config/git/config")
_GIT_CONFIG_COMMON_FILES: Final = ("config", "config.worktree")
_GIT_CONFIG_COMMON_GLOBS: Final = ("worktrees/*/config.worktree", "worktrees/*/config")
_GIT_CONFIG_REPO_FILES: Final = (".git/config", ".git/config.worktree")


def _git_config_file(abs_path: str, co: CheckoutGroup | None, rel: str | None, ctx: _Ctx) -> bool:
    ci = True
    folded = fold(abs_path, ci)
    if any(folded == fold(posixpath.join(ctx.home, f), ci) for f in _GIT_CONFIG_HOME_FILES) or folded == "/etc/gitconfig":
        return True
    for c in ctx.idx.checkouts:
        if is_under(abs_path, c.git_common_dir, case_insensitive=ci):
            crel = rel_to(abs_path, c.git_common_dir)
            if fold(crel, ci) in _GIT_CONFIG_COMMON_FILES:
                return True
            if any(compile_glob(g, ci).match(crel) for g in _GIT_CONFIG_COMMON_GLOBS):
                return True
    return rel is not None and fold(rel, ci) in _GIT_CONFIG_REPO_FILES


def _crew_policy(abs_path: str, co: CheckoutGroup | None, rel: str | None, ctx: _Ctx, op: str) -> bool:
    ci = True  # crew-policy locations are compared case-insensitively everywhere (safe on any volume)
    home_crew = posixpath.join(ctx.home, ".remembra")
    for g in ctx.policy_home_globs:
        if is_under(abs_path, ctx.home, case_insensitive=ci) and compile_glob(g, ci).match(rel_to(abs_path, ctx.home)):
            return True
    if is_under(abs_path, home_crew, case_insensitive=ci):
        return True
    roots: list[str] = [home_crew]
    for c in ctx.idx.checkouts:
        hooks = posixpath.join(c.git_common_dir, "hooks")
        if is_under(abs_path, hooks, case_insensitive=ci):
            return True
        roots += [hooks, posixpath.join(c.toplevel, ".remembra"), posixpath.join(c.git_common_dir, "config")]
    if co is not None and rel is not None:
        for g in ctx.policy_repo_globs:
            if compile_glob(g, ci).match(rel):
                return True
    if _git_config_file(abs_path, co, rel, ctx):
        return True
    if op == "remove":
        # removing an ancestor of a crew-policy location removes the policy too
        for r in roots:
            if is_under(r, abs_path, case_insensitive=ci) and fold(r, ci) != fold(abs_path, ci):
                return True
    return False


def _marker_file(abs_path: str, co: CheckoutGroup | None, rel: str | None, ctx: _Ctx) -> bool:
    if is_under(abs_path, ctx.home, case_insensitive=True):
        hrel = rel_to(abs_path, ctx.home)
        if any(compile_glob(g, True).match(hrel) for g in _MARKER_HOME_GLOBS):
            return True
    if co is not None and rel is not None:
        return any(compile_glob(g, co.case_insensitive).match(rel) for g in _MARKER_REPO_GLOBS)
    return False


def _edit_list(items: Iterable[Any], old_key: str, new_key: str) -> list[tuple[str, str, bool]]:
    out: list[tuple[str, str, bool]] = []
    for e in items:
        if isinstance(e, Mapping) and isinstance(e.get(old_key), str):
            out.append((e[old_key], str(e.get(new_key) or ""), bool(e.get("replace_all"))))
    return out


def _apply_edits(text: str, edits: Sequence[tuple[str, str, bool]]) -> str | None:
    """Simulate Claude Code's Edit/MultiEdit; ``None`` when an ``old_string`` is missing (the tool would fail)."""
    for old, new, replace_all in edits:
        if not old or old not in text:
            return None
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return text


def crew_hook_entries(text: str) -> set[tuple[str, ...]]:
    """Crew hook entries in a file: ``(event, matcher, command)`` for Claude settings JSON, else marker commands.

    A settings file that no longer parses as JSON yields ``{("<unparseable>",)}`` so that breaking the
    file (and with it every crew hook) counts as dropping the entries.
    """
    entries: set[tuple[str, ...]] = {(m,) for m in marker_entries(text)}
    if not entries:
        return set()
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return entries
    try:
        data = json.loads(text)
    except ValueError:
        return {("<unparseable>",)}
    out: set[tuple[str, ...]] = set()
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            for grp in groups if isinstance(groups, list) else ():
                if not isinstance(grp, dict):
                    continue
                matcher = str(grp.get("matcher") or "")
                for h in grp.get("hooks") or ():
                    cmd = h.get("command") if isinstance(h, dict) else None
                    if isinstance(cmd, str) and S.CREW_HOOK_MARKER in cmd:
                        out.add((str(event), matcher, cmd.strip()))
    # marker text outside a well-formed hooks block still counts (it was put there on purpose)
    return out or entries


# Claude Code settings keys that switch hooks off for every scope (user, project, project-local).
_HOOKS_OFF_KEYS: Final = ("disableAllHooks", "allowManagedHooksOnly")
_CLAUDE_SETTINGS_GLOB: Final = ".claude/settings*.json"
# Files that can hold crew hook entries; removing a directory above one that does removes the entries.
_MARKER_HOME_FILES: Final = (".claude/settings.json", ".claude/settings.local.json")
_MARKER_REPO_FILES: Final = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".husky/pre-commit",
    ".husky/pre-push",
    ".husky/prepare-commit-msg",
    "lefthook.yml",
    "lefthook.yaml",
    "lefthook-local.yml",
    "lefthook-local.yaml",
    ".lefthook.yml",
    ".lefthook.yaml",
    ".lefthook-local.yml",
    ".lefthook-local.yaml",
    ".config/lefthook.yml",
    ".config/lefthook.yaml",
    ".config/lefthook-local.yml",
    ".config/lefthook-local.yaml",
)


def _claude_settings_file(abs_path: str, rel: str | None, ctx: _Ctx) -> bool:
    if is_under(abs_path, ctx.home, case_insensitive=True) and compile_glob(_CLAUDE_SETTINGS_GLOB, True).match(
        rel_to(abs_path, ctx.home)
    ):
        return True
    return rel is not None and compile_glob(_CLAUDE_SETTINGS_GLOB, True).match(rel)


def switches_hooks_off(text: str) -> bool:
    """The settings JSON sets ``disableAllHooks`` (or ``allowManagedHooksOnly``), which silences the crew gate."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and any(bool(data.get(k)) for k in _HOOKS_OFF_KEYS)


def _settings_tamper(t: Target, real: str, rel: str | None, ctx: _Ctx) -> bool:
    """Surgical protection of crew hook entries (D28, §5.2 row 2).

    Tamper when an edit's ``old_string`` contains the crew marker, or when the file after the write or
    the simulated edit no longer has every crew hook entry it has now (same event, matcher and
    command). Other edits to the same file are allowed. A Bash or MCP write whose result cannot be
    known is tamper when the file currently holds crew entries.

    Claude settings files (``~/.claude/settings*.json`` and the repo's ``.claude/settings*.json``) are
    also tamper when the result sets ``disableAllHooks``/``allowManagedHooksOnly`` (that switches off
    the crew hooks of every scope, whichever file holds them). When a write's result cannot be
    known, only a file that holds crew entries is protected (other settings stay editable, §5.2).
    """
    if any(S.CREW_HOOK_MARKER in old for old, _new, _all in t.edits):
        return True
    current = ctx.fs.read_text(real)
    have = crew_hook_entries(current or "")
    claude = _claude_settings_file(real, rel, ctx)
    if t.content is not None:
        after = t.content
    elif t.edits:
        edited = _apply_edits(current or "", t.edits)
        if edited is None:
            return False  # an old_string is missing: the tool fails and nothing changes
        after = edited
    else:
        # Bash/MCP write or removal: the result is unknown. The parser flags a writing command whose
        # text sets a hooks-off key (``_HOOKS_OFF_RE``); anything else is judged by the entries.
        return bool(have)
    if claude and switches_hooks_off(after):
        return True
    return bool(have) and not have <= crew_hook_entries(after)


def _removes_hook_entries(abs_path: str, ctx: _Ctx) -> bool:
    """Removing (or moving away) a directory above a file that holds crew hook entries (``rm -rf ~/.claude``)."""
    candidates = [posixpath.join(ctx.home, f) for f in _MARKER_HOME_FILES]
    for c in ctx.idx.checkouts:
        candidates += [posixpath.join(c.toplevel, f) for f in _MARKER_REPO_FILES]
    for cand in dict.fromkeys(candidates):
        if fold(cand, True) == fold(abs_path, True) or not is_under(cand, abs_path, case_insensitive=True):
            continue
        if crew_hook_entries(ctx.fs.read_text(cand) or ""):
            return True
    return False


def _target_facts(t: Target, ctx: _Ctx) -> tuple[dict[str, Any], dict[str, Any]]:
    facts: dict[str, Any] = {}
    info: dict[str, Any] = {"target": t}
    if t.kind == "service":
        _service_facts(t, ctx, facts, info)
        return facts, info
    if t.kind == "zone":
        zones = ctx.idx.zones._with_ancestors(ctx.idx.zones.by_id[z] for z in t.zone_ids if z in ctx.idx.zones.by_id)
        _zone_facts(zones, [], None, ctx, facts, info, leaf_candidates=zones)
        return facts, info
    assert t.abs_path is not None
    real = ctx.fs.realpath(t.abs_path)  # symlinks: the write lands where the link points
    co = ctx.idx.locate(real)
    rel = rel_to(real, co.toplevel) if co is not None else None
    t.rel = rel
    t.checkout = co
    t.display = rel if rel is not None else _outside_display(real, ctx)
    if _crew_policy(real, co, rel, ctx, t.op) or (real != t.abs_path and _crew_policy(t.abs_path, co, rel, ctx, t.op)):
        facts["crew_policy_target"] = True
        info["policy"] = True
    if (_marker_file(real, co, rel, ctx) and _settings_tamper(t, real, rel, ctx)) or (
        t.op == "remove" and _removes_hook_entries(real, ctx)
    ):
        facts["tamper_command"] = True
        info["tamper_kinds"] = {"settings_hook_edit"}
    if co is None:
        if not facts:
            info["outside"] = True
        return facts, info
    ci = co.case_insensitive
    info["checkout"] = co
    if ctx.caller not in co.session_ids:
        foreign = [s for s in co.session_ids if ctx.idx.is_live_or_reserved(s)]
        if foreign:
            facts["foreign_checkout"] = True
            info["foreign_session"] = foreign[0]
    assert rel is not None
    zi = ctx.idx.zones
    if zi.ignored(rel, ci):
        facts["path_ignored"] = True
    exists = ctx.fs.exists(real)
    is_dir = t.kind == "dir" or ctx.fs.is_dir(real)
    if not is_dir:
        entry = zi.commons_entry(rel, ci)
        if entry is not None:
            kind = str(entry.get("kind") or "plain")
            facts["commons"] = True
            facts["commons_kind"] = kind
            facts["creates_file"] = not exists
            if kind == "append_only" or "migrations" in rel.split("/"):
                facts["migration"] = True
            if kind == "append_only" and exists:
                facts["append_only_edit_existing"] = True
            info["commons_glob"] = entry.get("glob")
    # clobber (row 5) and dirty elsewhere (row 16); a directory covers the footprints below it
    frel = fold(rel, ci)
    for fp in ctx.idx.footprints:
        sid = str(fp.get("session_id") or "")
        fpath = fold(str(fp.get("path", "")), ci)
        # a directory write clobbers other sessions' dirty files below it in this same worktree; tree
        # writers are judged by row 13 (the contract's first-match order), not per file
        below = (
            is_dir
            and t.op != "tree"
            and fp.get("worktree_id") == co.worktree_id
            and (frel == "." or fpath.startswith(frel + "/"))
        )
        if sid == ctx.caller or not (fpath == frel or below):
            continue
        if fp.get("worktree_id") == co.worktree_id:
            if fp.get("state") == "dirty" and ctx.idx.is_live(sid):
                facts["same_worktree_dirty_elsewhere"] = True
                info.setdefault("clobber_session", sid)
        elif fp.get("state") in ("dirty", "committed"):
            facts["dirty_in_other_checkout"] = True
            info.setdefault("dirty_session", sid)
    if not is_dir and not zi.match(rel, ci):
        # file claims cover only paths outside every zone: a zone path is governed by its zone's claims
        # (protected, frozen, reserve_for, task-for-parent), never by a path glob that happens to match it
        _file_claim_facts(rel, ci, ctx, facts, info)
    # zones
    if is_dir:
        containing = zi.containing_dir(rel, ci)
        blocking = zi.overlapping_dir(rel, ci) if t.overlap else []
        merged = {str(z["id"]): z for z in [*containing, *blocking]}
        _zone_facts(containing, list(merged.values()), rel, ctx, facts, info, leaf_candidates=containing)
    else:
        zones = zi.match(rel, ci)
        _zone_facts(zones, zones, rel, ctx, facts, info, leaf_candidates=zones)
    if facts.get("no_zone_match"):
        policy = ctx.idx.settings.get("undeclared_policy") or "footprint"
        facts["undeclared_policy"] = policy
        if policy == "file_claim" and not is_dir:
            info["claim_request"] = ClaimRequest(None, None, "exclusive", path_glob=rel)
    return facts, info


def _file_claim_facts(rel: str, ci: bool, ctx: _Ctx, facts: dict[str, Any], info: dict[str, Any]) -> None:
    """File-level claims (``path_glob``, from ``undeclared_policy: file_claim``) act like a zone of one path."""
    for c in ctx.idx.claims:
        glob = c.get("path_glob")
        if not glob or c.get("zone_id") or not compile_glob(str(glob), ci).match(rel):
            continue
        state = c.get("state")
        if ctx.is_mine(c) and state in LIVE_HOLD_STATES:
            facts["own_claim_lease_ok" if ctx.lease_ok(c) else "own_claim_lease_passed"] = True
            info.setdefault("claim", c)
        elif state == "reserved":
            if not ((ctx.is_mine(c) and c.get("reserved_for") in (None, ctx.caller)) or c.get("reserved_for") == ctx.caller):
                facts["reserved_for_other"] = True
                if any(o.get("claim_id") == c.get("id") and o.get("to_session") == ctx.caller for o in ctx.idx.offers):
                    facts["holds_offer"] = True
                _set_blocker(info, None, c, "reserved")
        elif state in LIVE_HOLD_STATES and not ctx.is_mine(c) and ctx.counts(c, None):
            if c.get("mode") == "exclusive":
                facts["exclusive_held_by_other"] = True
                _set_blocker(info, None, c, "exclusive")
            elif c.get("mode") == "shared":
                facts["shared_claim_by_others"] = True


def _zone_facts(
    zones: list[Mapping[str, Any]],
    blocking_zones: list[Mapping[str, Any]],
    rel: str | None,
    ctx: _Ctx,
    facts: dict[str, Any],
    info: dict[str, Any],
    *,
    leaf_candidates: list[Mapping[str, Any]],
) -> None:
    all_zones = {str(z["id"]): z for z in [*zones, *blocking_zones]}
    if not zones and not blocking_zones:
        facts["no_zone_match"] = True
        return
    zone_of = all_zones.get
    claims = ctx.zone_claims(all_zones)
    own_ids = {str(z["id"]) for z in zones}
    now = ctx.now
    # row 3: protected without a grant, or frozen by a human
    for z in all_zones.values():
        mine = any(ctx.is_mine(c) and c.get("state") in LIVE_HOLD_STATES and c.get("zone_id") == z["id"] for c in claims)
        if z.get("protected") and not mine:
            facts["zone_protected_no_grant"] = True
            info.setdefault("protected_zone", z)
            info.setdefault("zone", z)
        if z.get("frozen_by"):
            until = parse_ts(z.get("frozen_until"))
            if until is None or until > now:
                facts["zone_frozen"] = True
                info["frozen_zone"] = z
                info.setdefault("zone", z)
    # own claims (rows 7, 8): only zones that contain the target
    own = [
        c
        for c in claims
        if ctx.is_mine(c)
        and c.get("state") in LIVE_HOLD_STATES
        and c.get("mode") in ("exclusive", "shared")
        and c.get("zone_id") in own_ids
    ]
    if own:
        if any(ctx.lease_ok(c) for c in own):
            facts["own_claim_lease_ok"] = True
        else:
            facts["own_claim_lease_passed"] = True
        info.setdefault("zone", zone_of(str(own[0]["zone_id"])))
        info.setdefault("claim", own[0])
    # others
    for c in claims:
        if ctx.is_mine(c) and c.get("state") != "reserved":
            continue
        cz = zone_of(str(c.get("zone_id")))
        if not ctx.counts(c, cz):
            continue
        state = c.get("state")
        if c.get("mode") == "exclusive" and state in LIVE_HOLD_STATES and not ctx.is_mine(c):
            facts["exclusive_held_by_other"] = True
            _set_blocker(info, cz, c, "exclusive")
        elif state == "reserved":
            mine_reserved = (ctx.is_mine(c) and c.get("reserved_for") in (None, ctx.caller)) or c.get(
                "reserved_for"
            ) == ctx.caller
            if not mine_reserved:
                facts["reserved_for_other"] = True
                if any(o.get("claim_id") == c.get("id") and o.get("to_session") == ctx.caller for o in ctx.idx.offers):
                    facts["holds_offer"] = True
                _set_blocker(info, cz, c, "reserved")
        elif c.get("mode") == "shared" and state in LIVE_HOLD_STATES and not ctx.is_mine(c):
            facts["shared_claim_by_others"] = True
            info.setdefault("shared_zone", cz)
    # auto-claim / parent (rows 17, 18)
    if own:
        return
    leaves = [z for z in leaf_candidates if z.get("is_leaf", True) and not z.get("protected")]
    parents = [z for z in leaf_candidates if not z.get("is_leaf", True)]
    if leaves:
        leaf = sorted(leaves, key=lambda z: len(ctx.idx.zones.ancestors(str(z["id"]))), reverse=True)[0]
        facts["leaf_zone_unclaimed"] = True
        enabled, over_cap = _auto_claim_enabled(leaf, ctx)
        facts["auto_claim_enabled"] = enabled
        if over_cap:
            info["over_cap"] = True
        if str(leaf["id"]) in ctx.unconfirmed_zone_ids or any(
            ctx.is_mine(c) and c.get("unconfirmed") and c.get("zone_id") == leaf["id"] for c in ctx.idx.claims
        ):
            facts["unconfirmed_pending"] = True
        info.setdefault("zone", leaf)
        info["claim_request"] = ClaimRequest(str(leaf["id"]), str(leaf.get("slug")), str(leaf.get("mode") or "exclusive"))
    elif parents:
        facts["parent_zone_unclaimed"] = True
        info.setdefault("zone", parents[0])
    elif not facts.get("zone_protected_no_grant"):
        facts["no_zone_match"] = True


def _set_blocker(info: dict[str, Any], zone: Mapping[str, Any] | None, claim: Mapping[str, Any], kind: str) -> None:
    """Remember the most relevant blocker for the reason text (an exclusive holder beats a reservation)."""
    rank = {"exclusive": 2, "reserved": 1}
    if rank[kind] > rank.get(str(info.get("blocker_kind") or ""), 0):
        info["blocker_kind"] = kind
        info["zone"] = zone
        info["claim"] = claim
        info["holder_session_id"] = claim.get("holder_session_id")


def _auto_claim_enabled(zone: Mapping[str, Any], ctx: _Ctx) -> tuple[bool, bool]:
    """(enabled, over_cap): settings.auto_claim and zone.auto_claim and under the per-session exclusive cap."""
    settings = ctx.idx.settings
    if not settings.get("auto_claim", True) or not zone.get("auto_claim", True) or zone.get("protected"):
        return False, False
    if zone.get("mode", "exclusive") == "exclusive":
        cap = int(settings.get("max_exclusive_claims_per_session") or 3)
        held = sum(
            1 for c in ctx.idx.claims if ctx.is_mine(c) and c.get("mode") == "exclusive" and c.get("state") in BLOCKING_STATES
        )
        if held >= cap:
            return False, True
    return True, False


def _service_facts(t: Target, ctx: _Ctx, facts: dict[str, Any], info: dict[str, Any]) -> None:
    svc = t.service or ""
    info["service"] = svc
    zones_for = ctx.idx.zones.zones_with_service(svc)
    zone_ids = {str(z["id"]) for z in zones_for} | set(t.zone_ids)
    for c in ctx.idx.claims:
        if ctx.is_mine(c) or c.get("state") not in BLOCKING_STATES:
            continue
        z = ctx.idx.zones.by_id.get(str(c.get("zone_id") or ""))
        hit = c.get("resource") == svc or (c.get("zone_id") in zone_ids and c.get("mode") == "exclusive")
        if hit and ctx.counts(c, z):
            facts["service_claimed_by_other"] = True
            info.setdefault("claim", c)
            info.setdefault("holder_session_id", c.get("holder_session_id"))
            if z is not None:
                info.setdefault("zone", z)
    if not facts:
        info["outside"] = True


# ===========================================================================
# Agent-facing templates (ids, slugs and callsigns only; free text in the data block)
# ===========================================================================

_TAMPER_LABELS: Final[Mapping[str, str]] = {
    "env_crew_var": "changing crew environment variables",
    "no_verify": "skipping the git hooks",
    "hooks_path": "changing the git hooks path or git settings that run commands",
    "husky_off": "switching off Husky",
    "lefthook_off": "switching off lefthook",
    "crewd_kill": "stopping the crew daemon",
    "crew_files_removed": "removing crew files",
    "settings_hook_edit": "removing or switching off the crew hooks in a settings file",
}
_PATH_SAFE_RE: Final = re.compile(r"[^\w./@+,=%~ -]")


def _safe_path(path: str | None, limit: int = 100) -> str:
    if not path:
        return "this path"
    text = S.clip_item(path, limit)
    text = _PATH_SAFE_RE.sub("?", text)
    return text


def _age(ts: Any, now: datetime) -> str | None:
    dt = parse_ts(ts)
    if dt is None:
        return None
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 90:
        return f"{secs}s"
    if secs < 90 * 60:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def _clock(ts: Any, tz: tzinfo | None) -> str | None:
    dt = parse_ts(ts)
    if dt is None:
        return None
    local = dt.astimezone(tz) if tz is not None else datetime.fromtimestamp(dt.timestamp())
    return local.strftime("%H:%M")


def _data_block(items: Sequence[str]) -> str:
    clean = [S.clip_item(i) for i in items if i]
    if not clean:
        return ""
    return S.DATA_OPEN + " · ".join(clean) + S.DATA_CLOSE


def _holder_name(claim: Mapping[str, Any] | None, idx: SnapshotIndex, human: str) -> str:
    if claim is None:
        return "another session"
    if claim.get("holder_kind") == "human":
        return human
    return idx.callsign(str(claim.get("holder_session_id") or ""))


def _fit(text: str, channel: str, fallback: str) -> str:
    """Keep a template inside its channel rules (length, no destructive or bypass text); else the fallback."""
    if not S.check_agent_text(text, channel):
        return text
    return fallback


def render_deny_reason(
    verdict: Verdict,
    info: Mapping[str, Any],
    idx: SnapshotIndex,
    now: datetime,
    *,
    caller: str,
    human: str = "the owner",
    tz: tzinfo | None = None,
) -> str:
    """The §5.2 deny/ask reason (≤450 chars, deterministic, ids only; titles only inside the data block)."""
    t: Target | None = info.get("target")
    path = _safe_path(t.display if t else None)
    zone: Mapping[str, Any] | None = info.get("zone")
    slug = str(zone.get("slug")) if zone else "this zone"
    claim: Mapping[str, Any] | None = info.get("claim")
    holder = _holder_name(claim, idx, human)
    holder_sid = str(claim.get("holder_session_id") or "") if claim else ""
    task_id = str(claim.get("task_id") or "") if claim else ""
    task = idx.task_label(task_id)
    task_title = (idx.tasks.get(task_id) or {}).get("title") if task_id else None
    rule, variant = verdict.rule, verdict.variant
    head = "BLOCKED by Remembra Crew: "
    data = _data_block([f'{task} title: "{task_title}"']) if task and task_title else ""
    at = f"@{holder}" if claim and claim.get("holder_kind") != "human" else f"@{human}"
    text: str
    if rule == 1:
        text = (
            f"{head}your session {idx.callsign(caller)} is paused by {human}. Wait until {human} resumes it from the dashboard."
        )
    elif rule == 2 and variant == "tamper":
        kinds = sorted(info.get("tamper_kinds") or verdict.tamper_kinds or ())
        label = _TAMPER_LABELS.get(kinds[0], "switching off crew protection") if kinds else "switching off crew protection"
        text = (
            f"{head}this would switch off crew protection ({label}). Agents cannot do this. "
            f"If you think the gate is wrong, tell @{human} with crew_say and work on something else."
        )
    elif rule == 2:
        text = (
            f"{head}{path} is crew policy (zones, crew hooks and crew runtime files). Agents cannot change it. "
            f"Propose the change to @{human} with crew_say."
        )
    elif rule == 3 and variant == "frozen":
        fz = info.get("frozen_zone") or zone or {}
        text = f'{head}{path} is in zone "{fz.get("slug", slug)}", FROZEN by {human}. Do not edit it. Work elsewhere.'
    elif rule == 3:
        slug = str((info.get("protected_zone") or zone or {}).get("slug", slug))
        text = (
            f'{head}{path} is in zone "{slug}", which is protected. '
            f"Only {human} can grant access from the dashboard. Work elsewhere."
        )
    elif rule == 4:
        fs_id = info.get("foreign_session") or ""
        text = (
            f"{head}{path} is inside the checkout of {idx.callsign(fs_id)}. Edit files only in your own checkout. "
            f"Ask @{idx.callsign(fs_id)} with crew_say if you need a change there."
        )
    elif rule == 5:
        other = idx.callsign(info.get("clobber_session"))
        text = (
            f"{head}{path} has uncommitted changes by {other} in this same checkout. Do not edit it. "
            f"Ask @{other} with crew_say, or work in your own worktree."
        )
    elif rule == 8:
        text = (
            f"{head}lease unconfirmed, reconnecting; your uncommitted work is safe. "
            f"Work outside zone {slug} until the crew daemon reconnects."
        )
    elif rule == 9 and zone is None and claim is not None and claim.get("path_glob"):
        age = _age((idx.sessions.get(holder_sid) or {}).get("last_activity_at"), now)
        seen = f" · active {age} ago" if age else ""
        text = (
            f"{head}{path} is claimed EXCLUSIVELY by {holder}{seen}. Do not edit it. "
            f"Options: work on other files; ask {at} with crew_say; {human} can hand it over from the dashboard."
        )
    elif rule == 9:
        age = _age((idx.sessions.get(holder_sid) or {}).get("last_activity_at"), now)
        tail = f" for {task}" if task else ""
        seen = f" · active {age} ago" if age else ""
        text = (
            f'{head}{path} is in zone "{slug}", held EXCLUSIVELY by {holder}{tail}{seen}. Do not edit files in zone {slug}. '
            f"Options: work outside zone {slug}; ask {at} with crew_say; {human} can hand it over from the dashboard."
        )
    elif rule == 10:
        holder_s = idx.sessions.get(holder_sid) or {}
        when = _clock(holder_s.get("last_activity_at"), tz)
        why = claim.get("reserve_reason") if claim else None
        stopped = ", ".join(x for x in (f"{holder} stopped {when}" if when else f"{holder} stopped", why) if x)
        pickup = f"the next pickup of {task}" if task else f"the next pickup of zone {slug}"
        if variant == "reserved_offered":
            adopt = f" If you are continuing {task}, run: remembra-crew adopt {task}." if task else ""
            text = (
                f"{head}{path} is RESERVED for {pickup} ({stopped}). "
                f"This baton was offered to you in your session brief.{adopt} Otherwise work elsewhere."
            )
        else:
            text = (
                f"{head}{path} is RESERVED for {pickup} ({stopped}). You were not offered this baton. "
                f"Work elsewhere, or ask {human} to hand it to you from the dashboard."
            )
        data = ""
    elif rule == 11:
        text = (
            f"{head}{path} is an existing file in an append-only area. Do not edit existing migrations; add a new file instead."
        )
    elif rule == 13:
        text = (
            f"{head}this formatter or generator would rewrite files in zone {slug}, held EXCLUSIVELY by {holder}. "
            f"Run it only on paths in your own zones, or ask {at} with crew_say."
        )
    elif rule == 14:
        svc = info.get("service") or (f"zone {slug}" if zone else "this service")
        tail = f" for {task}" if task else ""
        text = f"{head}this uses {svc}, held by {holder}{tail}. Do not run it now. Ask {at} with crew_say."
    elif rule == 15:
        op = str(info.get("git_op") or "git").replace("_", " ")
        other = idx.callsign(info.get("holder_session_id"))
        text = (
            f"{head}a tree-wide git operation ({op}) would change files {other} is editing in this checkout. "
            f"Work in your own worktree or ask @{other} with crew_say."
        )
    elif rule in (17, 19) and variant.endswith("claim_conflict"):
        if info.get("winner"):
            winner = idx.callsign(info.get("winner"))
            text = (
                f'{head}{path} is in zone "{slug}", just claimed by {winner}. Do not edit files in zone {slug}. '
                f"Work elsewhere or ask @{winner} with crew_say."
            )
        else:
            text = (
                f'{head}{path} is in zone "{slug}", just claimed by another session. '
                f"Do not edit files in zone {slug}. Work elsewhere."
            )
    elif rule in (17, 19) and (variant.endswith("claim_cap") or (variant.endswith("claim_required") and info.get("over_cap"))):
        text = (
            f"{head}you already hold the maximum number of exclusive zones. Release one with "
            f'crew_claim(action="release") before working in zone {slug}.'
        )
    elif rule in (17, 19) and variant.endswith("claim_required"):
        text = f'{head}zone {slug} must be claimed first: call crew_claim(zone="{slug}") or crew_task(action="start").'
    elif rule in (17, 19) and variant.endswith("unconfirmed_pending"):
        text = f"{head}your claim on zone {slug} is not confirmed yet. Retry in a few seconds or work elsewhere."
    elif rule == 18:
        text = f'{head}{path} is in zone "{slug}", a parent zone. Claim it with a task: crew_task start (or work in a leaf zone).'
    else:
        text = f"{head}{path} cannot be written now (rule {rule}). Ask @{human} with crew_say."
    if rule not in (9, 13, 14):
        data = ""  # task context only where a task holds the zone or service
    full = f"{text}\n{data}" if data else text
    fallback = f"{head}this write is blocked by crew rules (rule {rule}). Work elsewhere or ask @{human} with crew_say."
    if len(full) > S.TEXT_CAPS["deny"] and data:
        full = text
    return _fit(full, "deny", fallback)


def stop_report_missing(task: str) -> str:
    """D16 Stop block: a finished-looking task without a completion report (server template, ids only)."""
    _check_task_ref(task)
    text = (
        f'Crew: task {task} looks finished but has no completion report. Call crew_report(task="{task}", '
        f"sections={{done, not_done, failing, next, follow_ups}}) or run: remembra-crew report {task}. "
        f'If it is not finished, call crew_task(action="update") with the current status. Then stop.'
    )
    return _fit(text, "stop", f"Crew: task {task} needs a completion report (crew_report). Then stop.")


def stop_breach(path_rel: str, zone_slug: str, holder_callsign: str, *, human_handle: str = "mani") -> str:
    """D16 Stop block for a *certain* exclusive breach by this session. Never a destructive instruction."""
    path = _safe_path(path_rel)
    slug = _safe_token(zone_slug)
    holder = _safe_token(holder_callsign)
    handle = _safe_token(human_handle)
    text = (
        f"Crew: your session wrote to {path}, which is in zone {slug} held by {holder}. Do not modify it further. "
        f"Tell @{holder} or @{handle} with crew_say what you changed and why, then stop."
    )
    return _fit(
        text,
        "stop",
        f"Crew: you wrote into zone {slug} held by {holder}. Do not modify it further; tell @{holder} with crew_say, then stop.",
    )


def _safe_token(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._:-]", "", text or "")[:48]
    return out or "unknown"


def _check_task_ref(task: str) -> None:
    if not re.fullmatch(S.TASK_REF_PATTERN, task or ""):
        raise ValueError(f"task must look like T-14, got {task!r}")


# ===========================================================================
# UserPromptSubmit digest and the crew lines
# ===========================================================================


def _lease_bucket(claim: Mapping[str, Any], now: datetime) -> int | None:
    exp = parse_ts(claim.get("lease_expires_at"))
    if exp is None:
        return None
    return int((exp - now).total_seconds() // 300)


def turn_digest(
    snapshot: Mapping[str, Any],
    caller: str,
    now: datetime | str | None = None,
    *,
    unread: int = 0,
    checkpoint_ids: Iterable[str] = (),
    obligations: Iterable[str] = (),
) -> str:
    """Digest over what the per-turn line reports (§8.2 UserPromptSubmit). Equal digest → print nothing.

    Inputs: others' claims, own claims (lease in 5-minute buckets), own task status, other sessions'
    states, unread items, others' new checkpoint ids, pending collisions, report obligations and
    offered batons.
    """
    now_dt = _now_dt(now)
    idx = SnapshotIndex(snapshot)
    others = sorted(
        (
            str(c.get("zone_id") or c.get("resource") or c.get("path_glob")),
            str(c.get("mode")),
            str(c.get("state")),
            str(c.get("holder_session_id") or c.get("holder_user_id")),
            str(c.get("task_id")),
            str(c.get("reserved_for")),
        )
        for c in idx.claims
        if c.get("holder_session_id") != caller
    )
    own = sorted(
        (
            str(c.get("zone_id") or c.get("resource") or c.get("path_glob")),
            str(c.get("state")),
            _lease_bucket(c, now_dt) or 0,
            bool(c.get("fenced")),
        )
        for c in idx.claims
        if c.get("holder_session_id") == caller
    )
    tasks = sorted((str(t["id"]), str(t.get("status"))) for t in idx.tasks.values() if t.get("owner_session_id") == caller)
    states = sorted((sid, str(s.get("state")), bool(s.get("stuck"))) for sid, s in idx.sessions.items() if sid != caller)
    collisions = sorted(
        str(c.get("id"))
        for c in snapshot.get("collisions") or ()
        if caller in (c.get("session_a"), c.get("session_b")) and c.get("state") in S.LIVE_COLLISION_STATES
    )
    offers = sorted(str(o.get("id")) for o in idx.offers if o.get("to_session") == caller)
    frozen = sorted(str(z.get("id")) for z in idx.zones.user_zones if z.get("frozen_by"))
    decisions = sorted(str(d.get("id")) for d in snapshot.get("decisions") or () if d.get("state") == "in_force")
    body = {
        "others": others,
        "own": own,
        "tasks": tasks,
        "states": states,
        "unread": [int(unread), dict(snapshot.get("inbox_counts") or {})],
        "checkpoints": sorted(str(x) for x in checkpoint_ids),
        "collisions": collisions,
        "obligations": sorted(str(x) for x in obligations),
        "offers": offers,
        "frozen": frozen,
        "decisions": decisions,
    }
    return hashlib.sha256(S.canonical_json(body)).hexdigest()[:32]


def render_you_line(
    snapshot: Mapping[str, Any],
    caller: str,
    now: datetime | str | None = None,
    *,
    files_since_checkpoint: int | None = None,
    tz: tzinfo | None = None,
) -> str:
    """``[crew <project> HH:MM] YOU: T-14 zone pos (lease ok) · 3 files since last checkpoint``."""
    now_dt = _now_dt(now)
    idx = SnapshotIndex(snapshot)
    ctx_like = _LeaseView(now_dt)
    project = _safe_token(str(idx.crew.get("project_id") or "project"))
    clock = _clock(now_dt.isoformat(), tz) or now_dt.strftime("%H:%M")
    parts: list[str] = []
    for t in sorted(idx.tasks.values(), key=lambda t: int(t.get("number") or 0)):
        if t.get("owner_session_id") == caller and t.get("status") in ("claimed", "in_progress", "blocked", "review"):
            parts.append(f"T-{t['number']} {t.get('status')}" if t.get("status") != "in_progress" else f"T-{t['number']}")
    for c in idx.claims:
        if c.get("holder_session_id") != caller:
            continue
        z = idx.zones.by_id.get(str(c.get("zone_id") or ""))
        what = f"zone {z.get('slug')}" if z else str(c.get("resource") or c.get("path_glob") or "claim")
        if c.get("state") == "reserved":
            state = "reserved for you"
        elif c.get("unconfirmed"):
            state = "unconfirmed"
        elif not ctx_like.ok(c):
            state = "lease unconfirmed"
        else:
            state = "lease ok"
        parts.append(f"{what} ({state})")
    # the caller answers for its sub-agents' claims (they hold them in their own name)
    for c in idx.claims:
        holder = str(c.get("holder_session_id") or "")
        if not holder or holder == caller or idx.parent_of(holder) != caller:
            continue
        z = idx.zones.by_id.get(str(c.get("zone_id") or ""))
        what = f"zone {z.get('slug')}" if z else str(c.get("resource") or c.get("path_glob") or "claim")
        parts.append(f"{what} (via sub-agent {idx.callsign(holder)})")
    you = " ".join(parts) if parts else "no claims"
    line = f"[crew {project} {clock}] YOU: {you}"
    if files_since_checkpoint:
        line += f" · {files_since_checkpoint} files since last checkpoint"
    return line


class _LeaseView:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def ok(self, claim: Mapping[str, Any]) -> bool:
        if claim.get("fenced"):
            return False
        exp = parse_ts(claim.get("lease_expires_at"))
        return exp is None or self.now < exp - timedelta(seconds=FENCE_MARGIN_S)


def render_do_not_touch(snapshot: Mapping[str, Any], caller: str, *, human: str = "the owner") -> str:
    """``DO NOT TOUCH: zone payroll → gemini-1 · zone billing (frozen by Mani)`` or ``""``."""
    idx = SnapshotIndex(snapshot)
    items: list[str] = []
    seen: set[str] = set()
    for c in idx.claims:
        if c.get("holder_session_id") == caller or c.get("state") not in BLOCKING_STATES:
            continue
        if c.get("mode") != "exclusive" and c.get("state") != "reserved":
            continue
        z = idx.zones.by_id.get(str(c.get("zone_id") or ""))
        what = f"zone {z.get('slug')}" if z else str(c.get("resource") or "")
        if not what or what in seen:
            continue
        seen.add(what)
        task = idx.task_label(str(c.get("task_id") or ""))
        if c.get("state") == "reserved":
            items.append(f"{what} → reserved" + (f" {task}" if task else ""))
        else:
            holder = human if c.get("holder_kind") == "human" else idx.holder_label(str(c.get("holder_session_id") or ""))
            items.append(f"{what} → {holder}" + (f" {task}" if task else ""))
    for z in idx.zones.user_zones:
        if z.get("frozen_by") and f"zone {z.get('slug')}" not in seen:
            items.append(f"zone {z.get('slug')} (frozen by {human})")
    return ("DO NOT TOUCH: " + " · ".join(items)) if items else ""


def render_turn(
    snapshot: Mapping[str, Any],
    caller: str,
    now: datetime | str | None = None,
    *,
    new_items: Sequence[str] = (),
    data_items: Sequence[str] = (),
    obligations: Sequence[str] = (),
    files_since_checkpoint: int | None = None,
    compact: bool = False,
    human: str = "the owner",
    tz: tzinfo | None = None,
) -> str:
    """The UserPromptSubmit delta (≤600 chars, ≤200 in compact mode; obligations always in full).

    ``new_items`` are server-template strings (ids, slugs and callsigns). ``data_items`` are agent-
    or repo-authored strings: they go only inside the data block, each clipped to 140 characters.
    """
    you = render_you_line(snapshot, caller, now, files_since_checkpoint=files_since_checkpoint, tz=tz)
    must = [_clean_line(o) for o in obligations if o]
    if compact:
        lines = [you, *must]
        text = "\n".join(lines)
        if len(text) > S.TEXT_CAPS["turn_compact"]:
            room = S.TEXT_CAPS["turn_compact"] - sum(len(m) + 1 for m in must)
            text = "\n".join([you[: max(20, room - 1)].rstrip() + "…", *must])
        return _fit(text, "turn_compact", you[:199])
    lines = [you]
    news = [_clean_line(n) for n in new_items if n]
    if news:
        lines.append("NEW: " + " · ".join(news))
    dnt = render_do_not_touch(snapshot, caller, human=human)
    if dnt:
        lines.append(dnt)
    lines.extend(must)
    block = _data_block(list(data_items))
    cap = S.TEXT_CAPS["turn"]
    while True:
        text = "\n".join(lines + ([block] if block else []))
        if len(text) <= cap:
            break
        if block and data_items:
            data_items = list(data_items)[:-1]
            block = _data_block(data_items)
            continue
        if len(lines) > 1 + len(must) and lines[1].startswith("NEW: "):
            lines[1] = lines[1][: max(10, len(lines[1]) - (len(text) - cap) - 1)] + "…"
            if len(lines[1]) <= 11:
                lines.pop(1)
            continue
        text = text[: cap - 1] + "…"
        break
    return _fit(text, "turn", you[:599])


def _clean_line(text: str) -> str:
    """Server template lines are single-line and tag-free."""
    return S.clip_item(text, 300)


# ===========================================================================
# Convenience for hook payloads
# ===========================================================================


def evaluate_hook_payload(
    payload: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any],
    caller: str,
    home: str,
    now: datetime | str | None = None,
    **kwargs: Any,
) -> Verdict:
    """Evaluate a Claude Code PreToolUse payload (``tool_name``, ``tool_input``, ``cwd``, ``permission_mode``)."""
    return evaluate(
        str(payload.get("tool_name") or ""),
        payload.get("tool_input") or {},
        snapshot=snapshot,
        caller=caller,
        cwd=str(payload.get("cwd") or os.getcwd()),
        home=home,
        now=now,
        permission_mode=str(payload.get("permission_mode") or "default"),
        **kwargs,
    )
