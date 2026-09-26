"""Build landing/llms-full.txt from the live pages, and hold landing/llms.txt to them.

remembra.dev/llms.txt (https://llmstxt.org) tells an agent what Remembra is,
how to set it up and where to read more. remembra.dev/llms-full.txt is the
text of the pages themselves, so an agent can answer from them instead of
from what it guesses. Neither may say anything the pages do not:

* llms-full.txt is generated here, verbatim, from the pages in SOURCES (the
  home page without its demos, install tabs and decorative parts; pricing,
  security, subprocessors, privacy and refunds; setup.md whole). Crew mode's
  page is left out: the feature is not available yet.
* every sentence of llms.txt outside its link lists must appear in that text
  (``unsupported()``), so a page edit that drops a claim fails the check.

    python scripts/site_llms.py          # rewrite landing/llms-full.txt
    python scripts/site_llms.py --check  # exit 1 if it is stale or llms.txt says more than the pages

tests/test_landing_site.py runs the check.
"""

from __future__ import annotations

import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

LANDING = Path(__file__).resolve().parents[1] / "landing"
SITE = "https://remembra.dev/"
LLMS = LANDING / "llms.txt"
FULL = LANDING / "llms-full.txt"

# (file in landing/, its URL): the pages llms-full.txt carries, in this order.
SOURCES = [
    ("setup.md", "https://remembra.dev/setup.md"),
    ("index.html", "https://remembra.dev/"),
    ("pricing.html", "https://remembra.dev/pricing"),
    ("security.html", "https://remembra.dev/security"),
    ("subprocessors.html", "https://remembra.dev/subprocessors"),
    ("privacy.html", "https://remembra.dev/privacy"),
    ("refunds.html", "https://remembra.dev/refunds"),
]

SKIP_TAGS = {"script", "style", "svg", "canvas", "figure", "button", "noscript", "template", "form", "nav", "aside", "select"}
# Parts of a page that are not its claims: the demo, the install tabs and command blocks
# (setup.md carries the commands; the hero's setup prompt for an agent, .cmd-prompt, stays),
# the price strip (the pricing page carries prices), and labels that repeat a heading.
KEEP_CLASSES = {"cmd-prompt"}
SKIP_CLASSES = {
    "cmd",
    "cmd-meta",
    "cmd-tabs",
    "hero-band",
    "const",
    "const-foot",
    "places",
    "prices",
    "chat",
    "n",
    "sr-only",
    "skip",
    "cells",
    "app-cap",
    "cta",
    "btn",
}
BLOCK = {"p", "li", "dt", "dd", "h1", "h2", "h3", "h4", "tr", "blockquote", "summary"}
HEADINGS = {"h1": "## ", "h2": "### ", "h3": "#### ", "h4": "##### "}
VOID = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}
ALT = {"m-only", "y-only"}  # the monthly and yearly figures of one price, side by side


class _Text(HTMLParser):
    """The readable text of a page's <main>, one block per line, links kept as markdown."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.in_main = False
        self.skip = 0  # 1 inside a skipped element (the one that started it is marked on the stack)
        self.stack: list[tuple[str, bool]] = []  # (tag, skipped here)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.kind: str | None = None
        self.href: list[str | None] = []
        self.cells: list[str] = []
        self.header_row = False
        self.table_rows = 0
        self.after_label = False  # just closed a <b>/<strong> label: a <span> right after it is its text

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split())
        self.buf = []
        if not text:
            return
        kind = self.kind or "p"
        if kind in HEADINGS:
            self.blocks.append("")
            self.blocks.append(HEADINGS[kind] + text)
            self.blocks.append("")
        elif kind in ("li", "dd"):
            self.blocks.append(("- " if kind == "li" else "  ") + text)
        elif kind == "dt":
            self.blocks.append("- " + text + ":")
        else:
            if self.blocks and self.blocks[-1].startswith(("- ", "  ")):
                self.blocks.append("")
            self.blocks.append(text)
            self.blocks.append("")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "main":
            self.in_main = True
            return
        if not self.in_main:
            return
        classes = set(a.get("class", "").split())
        skippable = tag in SKIP_TAGS or a.get("aria-hidden") == "true" or bool(classes & SKIP_CLASSES)
        starts_skip = not self.skip and skippable and not classes & KEEP_CLASSES
        if tag not in VOID:
            self.stack.append((tag, starts_skip))
            if starts_skip:
                self.skip = 1
        if self.skip or starts_skip:
            return
        if tag in BLOCK or tag in ("td", "th", "table", "ul", "ol", "dl", "section", "article", "div"):
            if tag in BLOCK:
                self._flush()
                self.kind = tag
            if tag == "table":
                self._flush()
                self.table_rows = 0
            if tag == "tr":
                self.cells = []
                self.header_row = False
            if tag in ("td", "th"):
                self.buf = []
                self.header_row = self.header_row or tag == "th"
        if tag == "span" and self.after_label:
            self.buf.append(": ")
        self.after_label = False
        if tag == "br":
            self.buf.append(" ")
        if tag == "code":
            self.buf.append("`")
        if tag == "a":
            href = a.get("href")
            self.href.append(urljoin(self.base, href) if href and not href.startswith("#") else None)
            if self.href[-1]:
                self.buf.append("[")
        if classes & ALT and "".join(self.buf).strip() and not "".join(self.buf).rstrip().endswith("·"):
            self.buf.append(" · ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._flush()
            self.in_main = False
            return
        if not self.in_main or tag in VOID:
            return
        # Pop to the matching open tag (the pages are well formed; this tolerates an unclosed <p>).
        while self.stack:
            open_tag, started_skip = self.stack.pop()
            if started_skip:
                self.skip = 0
            if open_tag == tag:
                break
        if self.skip:
            return
        self.after_label = tag in ("b", "strong")
        if tag == "code":
            self.buf.append("`")
        elif tag == "a":
            href = self.href.pop() if self.href else None
            if href:
                self.buf.append(f"]({href})")
        elif tag in ("td", "th"):
            self.cells.append(" ".join("".join(self.buf).split()))
            self.buf = []
        elif tag == "tr":
            if self.cells:
                self.blocks.append("| " + " | ".join(self.cells) + " |")
                if self.table_rows == 0 and self.header_row:
                    self.blocks.append("|" + "|".join(" --- " for _ in self.cells) + "|")
                self.table_rows += 1
            self.cells = []
            self.kind = None
        elif tag == "table":
            self.blocks.append("")
        elif tag in BLOCK:
            self._flush()
            self.kind = None

    def handle_data(self, data: str) -> None:
        if self.in_main and not self.skip:
            self.after_label = False
            self.buf.append(data)


def page_text(name: str, url: str) -> str:
    """One page's text as markdown: setup.md verbatim (headings one level down), an HTML page's <main> converted."""
    raw = (LANDING / name).read_text(encoding="utf-8")
    if name.endswith(".md"):
        return re.sub(r"^(#+) ", lambda m: "#" + m.group(1) + " ", raw, flags=re.M).strip()
    parser = _Text(url)
    parser.feed(raw)
    parser.close()
    text = "\n".join(parser.blocks)
    text = re.sub(
        r"\[([^\]]*)\]\(([^)]*)\)", lambda m: f"[{m.group(1).strip()}]({m.group(2)})" if m.group(1).strip() else "", text
    )
    text = re.sub(r"`\s*`", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build() -> str:
    parts = [
        "# Remembra: remembra.dev in full",
        "",
        "> The text of the pages on remembra.dev, for agents. Generated from the pages by "
        "scripts/site_llms.py; the pages govern. The short version is https://remembra.dev/llms.txt.",
    ]
    for name, url in SOURCES:
        parts += ["", "---", "", f"Source: {url}", "", page_text(name, url)]
    return "\n".join(parts).rstrip() + "\n"


def _plain(text: str) -> str:
    """Markdown and punctuation that do not change a claim, removed; whitespace collapsed; case folded."""
    text = html.unescape(text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "")
    text = re.sub(r"[“”]", '"', text).replace("’", "'")
    return " ".join(text.split()).lower()


def claims(llms: str) -> list[str]:
    """The sentences llms.txt states: its summary and instruction lines, not headings or link lists."""
    out: list[str] = []
    for line in llms.splitlines():
        body = line.strip()
        if not body or body.startswith("#") or re.match(r"^- \[[^\]]+\]\(", body):
            continue
        body = body.lstrip(">- ").strip()
        for sentence in re.split(r"(?<=[.;])\s+", body):
            sentence = sentence.strip().rstrip(".;:").strip()
            if sentence:
                out.append(sentence)
    return out


def unsupported(llms: str | None = None, full: str | None = None) -> list[str]:
    """Sentences of llms.txt that no page says (empty when every one is backed)."""
    corpus = _plain(full if full is not None else build())
    text = llms if llms is not None else LLMS.read_text(encoding="utf-8")
    return [s for s in claims(text) if _plain(s) not in corpus]


def main(argv: list[str]) -> int:
    check = "--check" in argv
    fresh = build()
    problems = []
    if check:
        if not FULL.is_file() or FULL.read_text(encoding="utf-8") != fresh:
            problems.append("landing/llms-full.txt is stale: run python scripts/site_llms.py")
    else:
        FULL.write_text(fresh, encoding="utf-8")
    for sentence in unsupported(full=fresh):
        problems.append(f"llms.txt says what no page says: {sentence!r}")
    for p in problems:
        print(p)
    if not problems:
        print("up to date" if check else f"wrote {FULL.relative_to(LANDING.parent)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
