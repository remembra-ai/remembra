"""Crew gatecore (WP-3): the stdlib-only decision core shared by the hook gate and the server guard.

This first part is the **Bash parser** (:func:`parse_bash`, §8.2): a quote-aware
shell lexer plus a per-command classifier that finds write targets, tree
writers, tree-wide git operations, tamper forms (row 2 of §5.2) and opaque
segments. It never executes anything and never runs a user-supplied regex.
Contract: ``docs/crew/bash-parser.md`` and ``tests/crew/vectors/bash/corpus.json``.

Stdlib only: the vendored gate (``crew-gate.py``, ``python -I``) imports it.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

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
            i = _scan_dquote(s, i + 1, Word())
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


def _scan_dollar(s: str, i: int, w: Word) -> int:
    """``s[i] == '$'``: consume one expansion into ``w`` and return the next index."""
    n = len(s)
    nxt = s[i + 1] if i + 1 < n else ""
    if nxt == "(":
        end = _scan_balanced(s, i + 2, "(", ")")
        w.has_subst = True
        w.raw += s[i:end]
        w.value += s[i:end]
        return end
    if nxt == "{":
        end = _scan_balanced(s, i + 2, "{", "}")
        w.has_var = True
        w.raw += s[i:end]
        w.value += s[i:end]
        return end
    if nxt and (nxt.isalnum() or nxt in "_@*#?$!-"):
        j = i + 1
        if nxt.isalpha() or nxt == "_":
            m = _NAME_RE.match(s, j)
            j = m.end() if m else j + 1
        else:
            j += 1
        w.has_var = True
        w.raw += s[i:j]
        w.value += s[i:j]
        return j
    w.raw += "$"
    w.value += "$"
    return i + 1


def _scan_dquote(s: str, i: int, w: Word) -> int:
    """Inside double quotes starting at ``i`` (just after the quote); return index after the closing quote."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == '"':
            w.raw += '"'
            return i + 1
        if c == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
            if s[i + 1] != "\n":
                w.value += s[i + 1]
            w.raw += s[i : i + 2]
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
            w.raw += s[i : j + 1]
            w.value += s[i : j + 1]
            i = j + 1
            continue
        w.raw += c
        w.value += c
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


def _mark_quote(w: Word) -> None:
    w.quoted = True
    if w.quote_start is None:
        w.quote_start = len(w.value)


def _lex_word(s: str, i: int) -> tuple[Word, int]:
    w = Word()
    n = len(s)
    brace_open = False
    brace_sep = False
    while i < n:
        c = s[i]
        if c in _WORD_BREAK:
            break
        if c == "\\":
            if i + 1 < n:
                if s[i + 1] == "\n":
                    i += 2
                    continue
                _mark_quote(w)
                w.value += s[i + 1]
                w.raw += s[i : i + 2]
                i += 2
            else:
                i += 1
            continue
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _LexError("unterminated single quote")
            _mark_quote(w)
            w.value += s[i + 1 : j]
            w.raw += s[i : j + 1]
            i = j + 1
            continue
        if c == "$" and s[i + 1 : i + 2] == "'":  # ANSI-C quoting: literal text
            j = i + 2
            buf = ""
            while j < n and s[j] != "'":
                if s[j] == "\\" and j + 1 < n:
                    buf += s[j : j + 2]
                    j += 2
                    continue
                buf += s[j]
                j += 1
            if j >= n:
                raise _LexError("unterminated $' quote")
            _mark_quote(w)
            w.value += buf
            w.raw += s[i : j + 1]
            i = j + 1
            continue
        if c == '"':
            _mark_quote(w)
            w.raw += '"'
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
            w.value += s[i : j + 1]
            w.raw += s[i : j + 1]
            i = j + 1
            continue
        if c in "*?" or (c == "[" and "]" in s[i + 1 : _word_end(s, i)]):
            w.has_glob = True
        elif c == "{":
            brace_open = True
        elif c == "," and brace_open or c == "." and brace_open and s[i : i + 2] == "..":
            brace_sep = True
        elif c == "}" and brace_open and brace_sep:
            w.has_brace = True
        w.value += c
        w.raw += c
        i += 1
    return w, i


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
    if res.opaque or res.writes or res.tree_writer or res.tamper or res.git_tree_op:
        res.read_only = False
    return res


def _raw_scan(text: str, res: BashParse) -> None:
    """Tamper markers in opaque code (``BASH_TAMPER_SCAN``); kill/removal markers need their verb in the same text."""
    res.scanned.append(text)
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

    # -- driver -------------------------------------------------------------
    def run(self) -> None:
        segs = self._segments()
        for seg in segs:
            if seg is None:
                continue
            self._command(seg)
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
        if scan:
            _raw_scan(seg.raw + ("\n" + extra if extra else ""), self.res)

    def _target(
        self, w: Word, *, cwd: str | None = None, remove: bool = False, is_dir: bool = False, restore: bool = False
    ) -> None:
        """Record a write target; dynamic targets make the command opaque instead."""
        base = self.cwd if cwd is None else cwd
        if w.dynamic:
            self.res.opaque = True
            self.all_read_only = False
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
        if redirect_write:
            self.all_read_only = False
        else:
            self.any_read_only = True

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
        if not w.dynamic and p._crew_path(w.value):
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
        if not w.dynamic and p._crew_path(w.value):
            p.res.tamper.add("crew_files_removed")
        p._target(w, is_dir=recursive)


def _h_truncate(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    _flags, pos, _ = _split_opts(args, ("-s", "-r", "--size", "--reference"))
    for w in pos:
        if not w.dynamic and p._crew_path(w.value):
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


def _h_sed(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    in_place = False
    have_script_opt = False
    pos: list[Word] = []
    i = 0
    while i < len(args):
        w = args[i]
        v = w.value
        if v == "--":
            pos.extend(args[i + 1 :])
            break
        if v.startswith("--"):
            if v.startswith("--in-place"):
                in_place = True
            elif v in ("--expression", "--file"):
                have_script_opt = True
                i += 1
            elif v.startswith(("--expression=", "--file=")):
                have_script_opt = True
            i += 1
            continue
        if v.startswith("-") and len(v) > 1 and not w.quoted:
            letters = v[1:]
            k = 0
            consumed_next = False
            while k < len(letters):
                ch = letters[k]
                if ch == "i":
                    in_place = True
                    if k == len(letters) - 1 and i + 1 < len(args) and args[i + 1].value == "" and args[i + 1].quoted:
                        consumed_next = True  # BSD: -i ''
                    break
                if ch in ("e", "f"):
                    have_script_opt = True
                    if k == len(letters) - 1:
                        consumed_next = True
                    break
                if ch == "l":
                    if k == len(letters) - 1:
                        consumed_next = True
                    break
                k += 1
            i += 2 if consumed_next else 1
            continue
        pos.append(w)
        i += 1
    files = pos if have_script_opt else pos[1:]
    if not in_place:
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    for w in files:
        p._target(w)


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
        if v.startswith("-") and len(v) > 1 and not w.quoted:
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
        if not w.dynamic and p._crew_path(w.value):
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


def _h_sort(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    out: str | None = None
    for i, w in enumerate(args):
        if w.value in ("-o", "--output") and i + 1 < len(args):
            out = args[i + 1].value
        elif w.value.startswith("--output="):
            out = w.value.split("=", 1)[1]
        elif w.value.startswith("-o") and len(w.value) > 2 and not w.value.startswith("--"):
            out = w.value[2:]
    if out is None:
        p._read_only_seg(seg, rw)
        return
    p.all_read_only = False
    p._target(Word(value=out, raw=out))


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


def _h_unset(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    for w in args:
        if w.value in S.CREW_ENV_TAMPER_VARS:
            p.res.tamper.add("env_crew_var")


def _h_kill(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    if any("crewd" in w.value for w in args):
        p.res.tamper.add("crewd_kill")
        p.all_read_only = False
        return
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


def _git_no_verify(args: list[Word], short_n_is_tamper: bool) -> bool:
    """``--no-verify`` anywhere in options; ``-n`` in a short-flag cluster for commit (a value flag ends the cluster)."""
    i = 0
    while i < len(args):
        v = args[i].value
        if v == "--":
            return False
        if v == "--no-verify":
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


def _h_git(p: _Parser, seg: _Seg, name: str, args: list[Word], rw: bool, hd: str | None) -> None:
    cwd = p.cwd
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
            if args[i + 1].value.lower().startswith("core.hookspath"):
                p.res.tamper.add("hooks_path")
            i += 2
            continue
        if v.startswith("--config-env"):
            val = v.split("=", 1)[1] if "=" in v else (args[i + 1].value if i + 1 < len(args) else "")
            if val.lower().startswith("core.hookspath"):
                p.res.tamper.add("hooks_path")
            i += 1 if "=" in v else 2
            continue
        if v in ("--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(args):
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


def _git_sub(p: _Parser, seg: _Seg, sub: str, rest: list[Word], vals: list[str], cwd: str | None, rw: bool) -> None:
    def ro() -> None:
        p._read_only_seg(seg, rw)

    if sub in _GIT_READ_ONLY_SUBS:
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
        ro() if vals[:1] == ["list"] else p._mark_opaque(seg)
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
        flags = [v for v in vals if v.startswith("-")]
        if any(f in ("-n", "--dry-run") or (not f.startswith("--") and "n" in f[1:]) for f in flags):
            ro()
        elif any(f == "--force" or (not f.startswith("--") and "f" in f[1:]) for f in flags):
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
        _flags, pos, values = _split_opts(rest, ("-s", "--source", "--conflict"))
        if "--pathspec-from-file" in values or not pos:
            p._mark_opaque(seg)
            return
        from_ref = "-s" in values or "--source" in values or any(f.startswith("-s") and len(f) > 2 for f in _flags)
        for w in pos:
            p._target(w, is_dir=not _has_ext(w.value), restore=not from_ref)
        return
    if sub == "rm":
        flags, pos, _ = _split_opts(rest, ("--pathspec-from-file",))
        if any(f in ("-n", "--dry-run") for f in flags):
            ro()
            return
        recursive = any(f == "-r" or (not f.startswith("--") and "r" in f[1:]) for f in flags)
        for w in pos:
            if not w.dynamic and p._crew_path(w.value):
                p.res.tamper.add("crew_files_removed")
            p._target(w, remove=True, is_dir=recursive)
        if not pos:
            p._mark_opaque(seg)
        return
    if sub == "mv":
        _flags, pos, _ = _split_opts(rest)
        if len(pos) < 2:
            p._mark_opaque(seg)
            return
        for w in pos[:-1]:
            if not w.dynamic and p._crew_path(w.value):
                p.res.tamper.add("crew_files_removed")
            p._target(w, remove=True)
        p._target(pos[-1])
        return
    if sub == "reset":
        if "--hard" in vals:
            p._git_op("reset_hard", cwd)
            p.all_read_only = False
        else:
            p._mark_opaque(seg)
        return
    if sub in ("rebase", "merge", "pull", "cherry-pick"):
        if sub in ("rebase", "merge", "pull") and "--no-verify" in vals:
            p.res.tamper.add("no_verify")
        p._git_op(sub.replace("-", "_"), cwd)
        p.all_read_only = False
        return
    if sub == "commit":
        if _git_no_verify(rest, short_n_is_tamper=True):
            p.res.tamper.add("no_verify")
        p._mark_opaque(seg)
        return
    if sub in ("push", "am"):
        if _git_no_verify(rest, short_n_is_tamper=False):
            p.res.tamper.add("no_verify")
        p._mark_opaque(seg)
        return
    p._mark_opaque(seg)


def _git_config(p: _Parser, seg: _Seg, vals: list[str], rw: bool) -> None:
    action: str | None = None
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
        if v in ("--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section"):
            action = "set"
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
        action, pos = "set", pos[1:]
    if action == "edit":
        p._mark_opaque(seg)
        return
    if action == "get" or (action is None and len(pos) <= 1):
        p._read_only_seg(seg, rw)
        return
    if pos and pos[0].lower() == "core.hookspath":
        p.res.tamper.add("hooks_path")
        p.all_read_only = False
        return
    p._mark_opaque(seg)


def _git_checkout(p: _Parser, seg: _Seg, rest: list[Word], cwd: str | None) -> None:
    values_opts = ("-b", "-B", "--orphan", "--conflict", "-t", "--track")
    before: list[Word] = []
    after: list[Word] | None = None
    switch = False
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
        if v in values_opts:
            i += 2
            continue
        if v.startswith("-") and v != "-":
            i += 1
            continue
        before.append(w)
        i += 1
    if switch:
        p._git_op("switch", cwd)
        p.all_read_only = False
        return
    if after is not None:
        for w in after:
            p._target(w, is_dir=not _has_ext(w.value), restore=not before)
        if not after:
            p._mark_opaque(seg)
        return
    if not before:
        p._mark_opaque(seg)
        return
    first = before[0]
    if len(before) == 1:
        if first.value == "." or (_has_ext(first.value) and first.value != "-"):
            p._target(first, is_dir=first.value == ".", restore=True)
        else:
            p._git_op("checkout_branch", cwd)
            p.all_read_only = False
        return
    no_ref = first.value == "." or _has_ext(first.value)
    paths = before if no_ref else before[1:]
    for w in paths:
        p._target(w, is_dir=not _has_ext(w.value), restore=no_ref)


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
