#!/usr/bin/env python3
"""Keep private material out of the public repository.

This repository is public: a file is published the moment a commit that
carries it is pushed, and deleting it in a later commit does not take it back.
This script refuses the kinds of material that have leaked before:

- private notes: remediation trackers under docs/audits/ and *-fixlog.md files;
- a built docs site (site/ is MkDocs output; docs are built from docs/ on
  deploy) and Vercel link folders;
- the 2026-03-15 feedback transcript, in any form (it quoted personal details);
- real names, products, customers or infrastructure in benchmark data;
- public IPv4 addresses (a server's address let anyone skip the CDN): use a
  documentation address (192.0.2.x, 198.51.100.x, 203.0.113.x) or a hostname;
- office documents (.docx and the like: unreviewable in a diff, and they
  carried confidential plans and the origin address), confidential markers,
  and production host details that belong in the private runbook;
- personal facts about the owner in fixtures (tests, scripts, benchmarks);
- a real-format Remembra API key (one sat in public history for months).

Some rules match words or phrases that must not be repeated here, so they are
stored as SHA-256 hashes of the lower-cased phrase (tokens [a-z0-9_]+ joined by
one space). `python scripts/ci/repo_hygiene.py hash "some phrase"` prints the
entry for a new one. The hashes keep the words out of the tree; they are not a
secret.

Modes:

    python scripts/ci/repo_hygiene.py tree                  # every tracked file (CI, tests)
    python scripts/ci/repo_hygiene.py range BASE..HEAD      # every commit in a range (CI on pull requests)
    python scripts/ci/repo_hygiene.py pre-push REMOTE [URL] # the commits a push would publish (hooks/pre-push)

In pre-push mode git passes one line per ref on stdin; every commit the push
would add to the remote is checked, not just the tip, because a commit that
adds a private file and a later one that deletes it would both be published.
Exit status: 0 clean, 1 findings (listed on stderr), 2 usage error.

Runs on the Python 3.9 that macOS ships, because the pre-push hook uses it.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40
MAX_TEXT_BYTES = 5 * 1024 * 1024  # larger files are not scanned for content


@dataclass(frozen=True)
class Finding:
    path: str
    line: int  # 0: the path itself
    rule: str
    message: str
    commit: str = ""

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        at = f" (commit {self.commit[:12]})" if self.commit else ""
        return f"{where}: [{self.rule}] {self.message}{at}"


# ---------------------------------------------------------------------------
# Path rules: files that must never be tracked, whatever they contain
# ---------------------------------------------------------------------------


OFFICE_EXTENSIONS = frozenset({"doc", "docx", "dotx", "ppt", "pptx", "xls", "xlsx", "odt", "ods", "odp", "rtf"})


def path_findings(path: str) -> list[Finding]:
    out: list[Finding] = []
    parts = path.split("/")
    name = parts[-1]
    if path.startswith(("docs/audits/", "docs/feedback/")) or name.endswith("-fixlog.md"):
        out.append(Finding(path, 0, "private-notes", "remediation trackers and fix logs stay in the private audits folder"))
    if path.startswith("site/"):
        out.append(Finding(path, 0, "built-site", "site/ is MkDocs build output; the docs are built from docs/ on deploy"))
    if ".vercel" in parts[:-1]:
        out.append(Finding(path, 0, "built-site", "a .vercel link folder is local state, not source"))
    if "." in name and name.rsplit(".", 1)[1].lower() in OFFICE_EXTENSIONS:
        out.append(
            Finding(path, 0, "office-document", "office documents are not reviewable in a diff; keep them in the private folder")
        )
    return out


# ---------------------------------------------------------------------------
# Content rules: text that must never be in a tracked file
# ---------------------------------------------------------------------------


TOKEN_RE = re.compile(r"[a-z0-9_]+")


def phrase_hash(phrase: str) -> str:
    return hashlib.sha256(" ".join(TOKEN_RE.findall(phrase.lower())).encode()).hexdigest()


@dataclass(frozen=True)
class PhraseRule:
    """Phrases, stored as (token count, hash of the first token, hash of the phrase)."""

    rule: str
    message: str
    phrases: tuple[tuple[int, str, str], ...]
    scope: tuple[str, ...] = ()  # path prefixes it applies to; empty: every file

    def applies(self, path: str) -> bool:
        return not self.scope or path.startswith(self.scope)


PHRASE_RULES: list[PhraseRule] = [
    PhraseRule(
        "feedback-transcript",
        "a copy of the 2026-03-15 feedback transcript (personal details); it stays in the private audits folder",
        (
            # the owner's account id, quoted in the transcript
            (
                1,
                "48bca82107e399b81283f99e50de2aca48afc3a61fbca4ff45e1c38a7bd7f847",
                "48bca82107e399b81283f99e50de2aca48afc3a61fbca4ff45e1c38a7bd7f847",
            ),
            # its title, and its file name
            (
                6,
                "94c5988df4b8912c3ea52aaaaff11c3e6d97e0d3fd13dd1719aafb5a87721e22",
                "3b226754f4352056c5d68f85548056bf13698fe7755b432bbf43fd43e340bccd",
            ),
            (
                6,
                "94c5988df4b8912c3ea52aaaaff11c3e6d97e0d3fd13dd1719aafb5a87721e22",
                "7134257913d08f1b28773032d0764556cbaa27601a507a3f740beab096f9a8e6",
            ),
        ),
    ),
    PhraseRule(
        "owner-private",
        "the owner's name, products, customers or infrastructure; benchmark data must be synthetic",
        (
            # names, the owner's other products and locale, the hosting stack, a trading symbol
            (
                1,
                "c6f7461d2b50c3cf8f5301940f54034be365ebbd2d6a13c79cd41507f807be34",
                "c6f7461d2b50c3cf8f5301940f54034be365ebbd2d6a13c79cd41507f807be34",
            ),
            (
                1,
                "be9b595a46e3c38dcca30e9650c3549b84ad9e8610ae4b07e7cd5bab960435f0",
                "be9b595a46e3c38dcca30e9650c3549b84ad9e8610ae4b07e7cd5bab960435f0",
            ),
            (
                1,
                "41e17ac4be19a198174f612840247f754439c380427190a8a4bed14ad6eb78cd",
                "41e17ac4be19a198174f612840247f754439c380427190a8a4bed14ad6eb78cd",
            ),
            (
                1,
                "61a881df2f426222462b7da88aaa5496f7db09ac19aaa48fa3fd9d90acb0b9d8",
                "61a881df2f426222462b7da88aaa5496f7db09ac19aaa48fa3fd9d90acb0b9d8",
            ),
            (
                1,
                "5894d70befd6310bd5bdcf6d9800b19fa121e72a6e42f34d84b2a1ac7f3d8f63",
                "5894d70befd6310bd5bdcf6d9800b19fa121e72a6e42f34d84b2a1ac7f3d8f63",
            ),
            (
                1,
                "f61622fdc7ab3ac33b32335b853f9889d486a0c5b2493353470ae3874c8bda0f",
                "f61622fdc7ab3ac33b32335b853f9889d486a0c5b2493353470ae3874c8bda0f",
            ),
            (
                1,
                "b48536abdfc554b923a184abfab609ecb8bb6548bceda90848e09250f6033dd3",
                "b48536abdfc554b923a184abfab609ecb8bb6548bceda90848e09250f6033dd3",
            ),
            (
                1,
                "4c9d0de73882db1ad3699a78fb522d3477e6e1e5ebac7cbfc607cd2a45f158cc",
                "4c9d0de73882db1ad3699a78fb522d3477e6e1e5ebac7cbfc607cd2a45f158cc",
            ),
            (
                1,
                "09dff240c4bc36c4d7b986904cf863e7de540c4ed1cd6ecfd7393cc837ab1784",
                "09dff240c4bc36c4d7b986904cf863e7de540c4ed1cd6ecfd7393cc837ab1784",
            ),
            (
                1,
                "7b8ad5d2420b1c7d41b678313a7460f7d3ec06890fbea4ded708c2cf09e0ef3c",
                "7b8ad5d2420b1c7d41b678313a7460f7d3ec06890fbea4ded708c2cf09e0ef3c",
            ),
            (
                1,
                "ff63d149bd73429d489fc9d1fa3cd1c5fb7b88a9c82694bb5e84ec700c4e0fad",
                "ff63d149bd73429d489fc9d1fa3cd1c5fb7b88a9c82694bb5e84ec700c4e0fad",
            ),
            (
                1,
                "46e66eeb8f554df8e4ab0c4a26944955a2db172093406fb11a3191aecf92352c",
                "46e66eeb8f554df8e4ab0c4a26944955a2db172093406fb11a3191aecf92352c",
            ),
            (
                1,
                "6d8564c8c3e742cbcd97831caf25ca6561f418bc4ed3dc5d07686af1c5df8874",
                "6d8564c8c3e742cbcd97831caf25ca6561f418bc4ed3dc5d07686af1c5df8874",
            ),
            (
                1,
                "d7ca8b3daaae05af8e8c47796bb39480946cf0cca2978dd996e622a8f1f6e313",
                "d7ca8b3daaae05af8e8c47796bb39480946cf0cca2978dd996e622a8f1f6e313",
            ),
            (
                1,
                "0693f1f346f5c6580de091f18e7d4231237f5af986126605a6ff973368fda8c8",
                "0693f1f346f5c6580de091f18e7d4231237f5af986126605a6ff973368fda8c8",
            ),
            (
                1,
                "3acd8d4d9cc1f3c68efc8eca95924aee98070f6e0a26bb1989971f5f57c19297",
                "3acd8d4d9cc1f3c68efc8eca95924aee98070f6e0a26bb1989971f5f57c19297",
            ),
        ),
        scope=("benchmarks/",),
    ),
    PhraseRule(
        "private-infra",
        "production host details (the hosting app's id, the SSH alias); they belong in the private runbook",
        (
            # the production app's id and its container-name prefix, and the SSH alias of the server
            (
                1,
                "3781929a8684aa70e0212f119c29a2858afb2e12dbf641be43fb28ae4d629c62",
                "3781929a8684aa70e0212f119c29a2858afb2e12dbf641be43fb28ae4d629c62",
            ),
            (
                1,
                "581c965eae961ee6997afd32c92a45d99bd0aabcbd9f189444427af9b4fbfe79",
                "581c965eae961ee6997afd32c92a45d99bd0aabcbd9f189444427af9b4fbfe79",
            ),
            (
                2,
                "7f5a55cf3f88be936fb9440249cb449f3067ccee4b525d0027dc9278a29c32c1",
                "9627c9c8e3b6e1e78da4f0a60c68dd047a5dc1a31d48a17384b0cbc220c9466c",
            ),
        ),
    ),
    PhraseRule(
        "owner-personal",
        "a personal fact about the owner (city, trade, personal git remote); use neutral names in fixtures",
        (
            # the owner's city, trade and personal GitHub remotes
            (
                1,
                "7b8ad5d2420b1c7d41b678313a7460f7d3ec06890fbea4ded708c2cf09e0ef3c",
                "7b8ad5d2420b1c7d41b678313a7460f7d3ec06890fbea4ded708c2cf09e0ef3c",
            ),
            (
                1,
                "0693f1f346f5c6580de091f18e7d4231237f5af986126605a6ff973368fda8c8",
                "0693f1f346f5c6580de091f18e7d4231237f5af986126605a6ff973368fda8c8",
            ),
            (
                2,
                "6c1ff09db3a73dc4a854f695d20d174a848d55f2d743bab2ee1f8fc75be454f3",
                "f234dbf4d4acfffd856e1194fbe6f23468c9100bc01c8965a0d1648857e893ed",
            ),
            (
                1,
                "d8695505f657990af45916b15b528d85faacefe250cc30c14835ebb036f2bcc4",
                "d8695505f657990af45916b15b528d85faacefe250cc30c14835ebb036f2bcc4",
            ),
            (
                2,
                "4c9d0de73882db1ad3699a78fb522d3477e6e1e5ebac7cbfc607cd2a45f158cc",
                "48b8052919ecc5b9a0283c837218b6340931eeb7076fa5b9bf6d677d39b41926",
            ),
            # the owner's other city (LEAK-8 review: a billing fixture moved "the office" there)
            (
                2,
                "b8e273995fc530537ce44478d8a2e617dc4aa01de28bab336e7fd746db027211",
                "08bb6a6c12d13454cbbb62f090171ab6071c986cfa0df19ce41acce251ff8b32",
            ),
        ),
        scope=("tests/", "scripts/", "benchmarks/"),
    ),
]

_token_hashes: dict[str, str] = {}


def _token_hash(token: str) -> str:
    h = _token_hashes.get(token)
    if h is None:
        h = _token_hashes[token] = hashlib.sha256(token.encode()).hexdigest()
    return h


def _phrase_findings(path: str, lines: list[str]) -> list[Finding]:
    rules = [r for r in PHRASE_RULES if r.applies(path)]
    if not rules:
        return []
    starts = {first for r in rules for _, first, _ in r.phrases}
    out: list[Finding] = []
    for lineno, line in enumerate(lines, 1):
        tokens = TOKEN_RE.findall(line.lower())
        for i, token in enumerate(tokens):
            if _token_hash(token) not in starts:
                continue
            for rule in rules:
                for n, first, full in rule.phrases:
                    if first == _token_hashes[token] and i + n <= len(tokens):
                        phrase = token if n == 1 else " ".join(tokens[i : i + n])
                        if n == 1 or hashlib.sha256(phrase.encode()).hexdigest() == full:
                            finding = Finding(path, lineno, rule.rule, rule.message)
                            if finding not in out:
                                out.append(finding)
    return out


IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)(/\d{1,2})?")
DOC_NETS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
# Public addresses tests use as obvious placeholders, never a server of ours.
ALLOWED_IPS = frozenset(
    {
        "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9",  # public resolvers
        "1.2.3.4", "6.6.6.6",  # placeholders in spoofing tests
        "93.184.216.34",  # example.com, in SSRF tests
        "162.158.10.20",  # inside a CDN's published edge range, in client-IP tests
        "198.51.101.1", "198.51.101.7", "198.51.102.7",  # "another /24" next to 198.51.100.0/24
    }
)  # fmt: skip
LOCK_FILES = ("uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Cargo.lock")


def _ip_findings(path: str, lines: list[str]) -> list[Finding]:
    if path.rsplit("/", 1)[-1] in LOCK_FILES:  # version strings look like addresses
        return []
    out: list[Finding] = []
    for lineno, line in enumerate(lines, 1):
        for match in IPV4_RE.finditer(line):
            text, cidr = match.group(1), match.group(2)
            if cidr or text in ALLOWED_IPS:  # a published range, not a host
                continue
            try:
                ip = ipaddress.IPv4Address(text)
            except ValueError:
                continue
            if not ip.is_global or ip.is_multicast or any(ip in net for net in DOC_NETS):
                continue
            out.append(
                Finding(
                    path,
                    lineno,
                    "public-ip",
                    "a public IPv4 address; use a documentation address (192.0.2.x, 198.51.100.x, 203.0.113.x) or a hostname",
                )
            )
            break
    return out


CONFIDENTIAL_RE = re.compile(r"\bCONFIDENTIAL\b")  # the marker, upper case; ordinary prose is fine


def _marker_findings(path: str, lines: list[str]) -> list[Finding]:
    return [
        Finding(
            path, lineno, "confidential", "a document marked confidential (the upper-case marker); keep it in the private folder"
        )
        for lineno, line in enumerate(lines, 1)
        if CONFIDENTIAL_RE.search(line)
    ]


# A Remembra API key is "rem_" + token_urlsafe(32): 40+ random characters, mixed case with digits.
# Placeholders ("rem_xxxx...", "rem_...") and long identifiers are not keys (LEAK-2).
REMEMBRA_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])rem_([A-Za-z0-9_-]{40,})")


def _generated(value: str) -> bool:
    return any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value)


def _key_findings(path: str, lines: list[str]) -> list[Finding]:
    return [
        Finding(
            path,
            lineno,
            "api-key",
            "a Remembra API key (rem_ and 40+ random characters): revoke it on the server and remove it; "
            "tests assemble fake keys at runtime",
        )
        for lineno, line in enumerate(lines, 1)
        if any(_generated(m.group(1)) for m in REMEMBRA_KEY_RE.finditer(line))
    ]


def content_findings(path: str, text: str) -> list[Finding]:
    lines = text.splitlines()
    return _phrase_findings(path, lines) + _ip_findings(path, lines) + _marker_findings(path, lines) + _key_findings(path, lines)


def _is_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def check_file(path: str, data: bytes | None) -> list[Finding]:
    found = path_findings(path)
    if data is not None and len(data) <= MAX_TEXT_BYTES and _is_text(data):
        found += content_findings(path, data.decode("utf-8", errors="replace"))
    return found


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str, stdin: str | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, text=True, check=False, encoding="utf-8")
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _toplevel(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def check_tree(root: Path) -> list[Finding]:
    """Every tracked file, as it is in the working tree."""
    root = _toplevel(Path(root))
    found: list[Finding] = []
    for path in _git(root, "ls-files", "-z").split("\0"):
        if not path:
            continue
        full = root / path
        data = full.read_bytes() if full.is_file() and not full.is_symlink() else None
        found += check_file(path, data)
    return found


def _changed_blobs(root: Path, commit: str) -> Iterator[tuple[str, str, list[str]]]:
    """(path, blob, parent blobs) for every file a commit adds or changes.

    A merge is diffed against all its parents at once (-c), so only content that
    none of the parents had (a conflict resolution) counts; what the merge brings
    in from the other side was either checked in its own commits or is already
    on the remote.
    """
    raw = _git(root, "diff-tree", "-r", "-c", "--root", "--no-commit-id", "--no-renames", "-z", commit)
    fields = raw.split("\0")
    i = 0
    while i < len(fields) - 1:
        header, path = fields[i], fields[i + 1]
        i += 2
        if not header.startswith(":"):
            continue
        parents = len(header) - len(header.lstrip(":"))
        tokens = header.lstrip(":").split()
        # modes (parents + 1), shas (parents + 1), status
        mode, blob = tokens[parents], tokens[2 * parents + 1]
        if blob == ZERO_SHA or mode == "160000":  # deleted, or a submodule
            continue
        yield path, blob, [b for b in tokens[parents + 1 : 2 * parents + 1] if b != ZERO_SHA]


def _read_blobs(root: Path, blobs: Iterable[str]) -> dict[str, bytes]:
    wanted = list(dict.fromkeys(blobs))
    if not wanted:
        return {}
    done = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input="".join(f"{b}\n" for b in wanted).encode(),
        capture_output=True,
        check=True,
    )
    out: dict[str, bytes] = {}
    buf = done.stdout
    pos = 0
    for blob in wanted:
        end = buf.index(b"\n", pos)
        header = buf[pos:end].decode().split()
        pos = end + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        size = int(header[2])
        out[blob] = buf[pos : pos + size]
        pos += size + 1
    return out


def check_commits(root: Path, commits: Iterable[str]) -> list[Finding]:
    """Findings in what these commits add.

    A path rule applies to every file a commit adds or changes. A content rule
    applies only to lines the file's parent version did not have: a line that
    was already there was checked in the commit that added it, or is already on
    the remote, and the tree check (CI) still reports it until it is removed.
    """
    root = _toplevel(Path(root))
    changes: list[tuple[str, str, str, list[str]]] = []
    for commit in commits:
        changes += [(commit, path, blob, parents) for path, blob, parents in _changed_blobs(root, commit)]
    contents = _read_blobs(root, [b for _, _, blob, parents in changes for b in (blob, *parents)])
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for commit, path, blob, parents in changes:
        if (path, blob) in seen:
            continue
        seen.add((path, blob))
        data = contents.get(blob)
        lines = data.decode("utf-8", errors="replace").splitlines() if data is not None else []
        old: set[str] = set()
        for parent in parents:
            if parent in contents:
                old.update(contents[parent].decode("utf-8", errors="replace").splitlines())
        for f in check_file(path, data):
            if f.line and 0 < f.line <= len(lines) and lines[f.line - 1] in old:
                continue
            found.append(Finding(f.path, f.line, f.rule, f.message, commit))
    return found


def commits_in_range(root: Path, spec: str) -> list[str]:
    return [c for c in _git(root, "rev-list", "--reverse", spec).split() if c]


def commits_to_push(root: Path, remote: str, ref_lines: Iterable[str]) -> list[str]:
    """The commits a push would add to the remote, from the lines git gives pre-push on stdin."""
    commits: list[str] = []
    seen: set[str] = set()
    for line in ref_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA:  # deleting a remote ref publishes nothing
            continue
        known = remote_sha != ZERO_SHA and (
            subprocess.run(["git", "cat-file", "-e", f"{remote_sha}^{{commit}}"], cwd=root, capture_output=True).returncode == 0
        )
        if known:
            spec = [local_sha, f"^{remote_sha}"]
        else:
            # New branch (or a remote tip we have not fetched): everything the remote's known refs do not have.
            spec = [local_sha, "--not", f"--remotes={remote}"]
        for commit in _git(root, "rev-list", "--reverse", *spec).split():
            if commit not in seen:
                seen.add(commit)
                commits.append(commit)
    return commits


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _report(found: list[Finding], what: str) -> int:
    if not found:
        return 0
    print(f"repo_hygiene: {what} would publish private material:", file=sys.stderr)
    for f in found:
        print(f"  {f}", file=sys.stderr)
    print(
        "Move the material to the private audits folder (or rewrite the commits that add it), then try again.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("tree", help="check every tracked file")
    rng = sub.add_parser("range", help="check every commit in a revision range")
    rng.add_argument("spec", help="for example origin/main..HEAD")
    push = sub.add_parser("pre-push", help="check what a push would publish (ref lines on stdin)")
    push.add_argument("remote")
    push.add_argument("url", nargs="?")
    hsh = sub.add_parser("hash", help="print the PhraseRule entry for a phrase")
    hsh.add_argument("phrase")
    args = parser.parse_args(argv)

    if args.mode == "hash":
        tokens = TOKEN_RE.findall(args.phrase.lower())
        if not tokens:
            return 2
        print(f'({len(tokens)}, "{_token_hash(tokens[0])}", "{phrase_hash(args.phrase)}"),')
        return 0

    root = _toplevel(Path(os.getcwd()))
    if args.mode == "tree":
        return _report(check_tree(root), "the tracked tree")
    if args.mode == "range":
        return _report(check_commits(root, commits_in_range(root, args.spec)), f"the commits in {args.spec}")
    commits = commits_to_push(root, args.remote, sys.stdin.read().splitlines())
    return _report(check_commits(root, commits), f"this push to {args.remote}")


if __name__ == "__main__":
    sys.exit(main())
