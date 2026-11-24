"""The blog pipeline (scripts/site_blog.py): Markdown posts in content/blog/ become pages in landing/blog/.

Every test builds into pytest's tmp_path from fixture posts, or checks the committed tree with --check, which
renders as of the date recorded in content/blog/build.json. Nothing here reads the clock or the network.
"""

from __future__ import annotations

import datetime as dt
import html
import importlib.util
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LANDING = ROOT / "landing"
CONTENT = ROOT / "content" / "blog"
WORKFLOWS = ROOT / ".github" / "workflows"
LEGACY = [
    "remembra-vs-mem0-vs-zep",
    "building-remembra-with-ai-agents",
    "multi-agent-orchestration-patterns",
    "why-your-ai-forgets-everything",
]
DESCRIPTION = "A fixture description that is comfortably longer than the fifty character minimum."
BUILD_DAY = dt.date(2026, 6, 1)


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


partials = _script("site_partials")
blog = _script("site_blog")


def _source(
    title: str = "A fixture post",
    date: str = "2026-05-01",
    body: str = "Some words for the body.\n\n## A heading\n\nMore words.",
    description: str = DESCRIPTION,
    extra: str = "",
) -> str:
    front = [f"title: {title}", f"description: {description}", f"date: {date}", "tags: [relay, handoffs]"]
    return "---\n" + "\n".join(line for line in [*front, extra] if line) + "\n---\n\n" + body + "\n"


@pytest.fixture()
def site(tmp_path: Path) -> Any:
    """A copy of what the blog needs: the legacy pages and manifest, the real sitemap, and no posts yet."""
    paths = blog.Paths(content=tmp_path / "content" / "blog", landing=tmp_path / "landing")
    paths.content.mkdir(parents=True)
    paths.blog.mkdir(parents=True)
    shutil.copy(CONTENT / "legacy.json", paths.legacy)
    for slug in LEGACY:
        shutil.copy(LANDING / "blog" / f"{slug}.html", paths.blog / f"{slug}.html")
    # The committed sitemap lists the posts published so far. This copy holds none of
    # their sources, so its blog lines are rebuilt for what it does hold: the legacy posts.
    committed = (LANDING / "sitemap.xml").read_text(encoding="utf-8")
    legacy_only = blog.ordered(blog.load_legacy(paths), BUILD_DAY)
    paths.sitemap.write_text(blog.render_sitemap(committed, legacy_only, BUILD_DAY), encoding="utf-8")
    return paths


def _add(paths: Any, name: str, **kwargs: str) -> Path:
    kwargs.setdefault("date", name[:10])
    path = paths.content / name
    path.write_text(_source(**kwargs), encoding="utf-8")
    return path


def _ld(page: str) -> list[dict[str, Any]]:
    return [json.loads(body) for body in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]


# ---------------------------------------------------------------------------
# The Markdown subset
# ---------------------------------------------------------------------------


def test_markdown_renders_headings_with_stable_ids_and_paragraphs() -> None:
    md = blog.Markdown()
    out = md.render("First line\nsecond line.\n\n## Why it works\n\n### Why it works\n\n#### Fine *print*\n")
    assert out == (
        "<p>First line\nsecond line.</p>\n"
        '<h2 id="why-it-works">Why it works</h2>\n'
        '<h3 id="why-it-works-2">Why it works</h3>\n'
        '<h4 id="fine-print">Fine <em>print</em></h4>'
    )
    assert md.ids == ["why-it-works", "why-it-works-2", "fine-print"]
    # The same source always gives the same ids, and a heading never takes an id the page itself uses.
    assert blog.render_markdown("## Why it works") == '<h2 id="why-it-works">Why it works</h2>'
    assert blog.render_markdown("## Main") == '<h2 id="main-2">Main</h2>'


def test_markdown_inline_marks() -> None:
    out = blog.render_markdown("Use **bold**, *italic*, _also italic_, `code` and [a link](https://example.com/a_b_c).")
    assert out == (
        "<p>Use <strong>bold</strong>, <em>italic</em>, <em>also italic</em>, <code>code</code> "
        'and <a href="https://example.com/a_b_c">a link</a>.</p>'
    )
    assert blog.render_markdown("snake_case_name and 2 * 3 * 4") == "<p>snake_case_name and 2 * 3 * 4</p>"
    assert blog.render_markdown(r"\*not emphasis\* and \`not code\`") == "<p>*not emphasis* and `not code`</p>"
    assert blog.render_markdown("[**bold** `code`](/pricing)") == (
        '<p><a href="/pricing"><strong>bold</strong> <code>code</code></a></p>'
    )
    assert blog.render_markdown("line one  \nline two") == "<p>line one<br>\nline two</p>"


def test_markdown_escapes_raw_html_everywhere() -> None:
    out = blog.render_markdown(
        '<script>alert("x")</script> & <b>bold</b>\n\n## <img src=x onerror=1>\n\n- <i>item</i>\n\n> <u>quote</u>\n\n'
        "`<code>` and [<em>text</em>](/a?b=1&c=2)\n\n| <th> |\n| --- |\n| <td> |\n"
    )
    assert "<script" not in out and "<b>" not in out and "<img" not in out and "<i>" not in out and "<u>" not in out
    assert '&lt;script&gt;alert("x")&lt;/script&gt; &amp; &lt;b&gt;bold&lt;/b&gt;' in out
    assert "<code>&lt;code&gt;</code>" in out
    assert '<a href="/a?b=1&amp;c=2">&lt;em&gt;text&lt;/em&gt;</a>' in out
    assert "<th>&lt;th&gt;</th>" in out and "<td>&lt;td&gt;</td>" in out
    # Site markers cannot be smuggled in either: the partials script would fill them.
    marked = blog.render_markdown("<!-- @lockup --><!-- /@lockup -->")
    assert "<!--" not in marked and partials.fill_snippets(marked) == marked
    assert blog.render_markdown('[x](https://example.com "a <b> & c")') == (
        '<p><a href="https://example.com" title="a &lt;b&gt; &amp; c">x</a></p>'
    )


def test_markdown_attribute_values_cannot_break_out() -> None:
    out = blog.render_markdown('[x](https://example.com/?q="><script>) and ![a "b" <c>](/img.png)')
    assert "<script" not in out
    assert 'href="https://example.com/?q=&quot;&gt;&lt;script&gt;"' in out
    assert 'alt="a &quot;b&quot; &lt;c&gt;"' in out and 'src="/img.png"' in out


def test_code_fences_keep_their_content_exactly() -> None:
    code = (
        'def f(a, b):\n\treturn a < b  # <b> & "q" **not bold** `x` [no](link)\n\n    # indented\n## not a heading\n- not a list'
    )
    out = blog.render_markdown(f"Before.\n\n```python\n{code}\n```\n\nAfter.")
    m = re.search(r'<pre><code class="language-python">(.*?)</code></pre>', out, re.S)
    assert m is not None
    assert html.unescape(m.group(1)) == code
    assert "<strong>" not in m.group(1) and "<h2" not in m.group(1) and "<li>" not in m.group(1)
    assert out.startswith("<p>Before.</p>\n<pre>") and out.endswith("</pre>\n<p>After.</p>")
    # No language, a tilde fence, and a fence that holds a shorter fence.
    assert blog.render_markdown("```\na\n```") == "<pre><code>a</code></pre>"
    assert blog.render_markdown("~~~sh\necho hi\n~~~") == '<pre><code class="language-sh">echo hi</code></pre>'
    assert blog.render_markdown("````md\n```\nx\n```\n````") == '<pre><code class="language-md">```\nx\n```</code></pre>'


def test_markdown_lists_quotes_and_tables() -> None:
    assert blog.render_markdown("- one\n- two\n  continued\n\n- three") == (
        "<ul>\n<li>one</li>\n<li>two\ncontinued</li>\n<li>three</li>\n</ul>"
    )
    assert blog.render_markdown("1. first\n2. second") == "<ol>\n<li>first</li>\n<li>second</li>\n</ol>"
    assert blog.render_markdown("> quoted **text**\n> more") == (
        "<blockquote>\n<p>quoted <strong>text</strong>\nmore</p>\n</blockquote>"
    )
    table = blog.render_markdown("| Tool | Price |\n| --- | :-: |\n| A | free |\n| B \\| C |")
    assert table == (
        "<table>\n<thead>\n<tr><th>Tool</th><th>Price</th></tr>\n</thead>\n<tbody>\n"
        "<tr><td>A</td><td>free</td></tr>\n<tr><td>B | C</td><td></td></tr>\n</tbody>\n</table>"
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("# A second h1", "start headings at ##"),
        ("##### Too deep", "headings go from ## to ####"),
        ("```\nnever closed", "never closed"),
        ("- a\n  - nested", "nested lists"),
        ("[x](javascript:alert(1))", "is not allowed"),
        ("[x](http://example.com)", "is not allowed"),
        ("[x](//evil.example)", "is not allowed"),
        ("[x](other-post.html)", "is not allowed"),
        ("![](/img.png)", "alt text"),
        ("![remote](https://example.com/a.png)", "must be a file on this site"),
        ("---", "horizontal rules"),
        ('```bad"lang\nx\n```', "not a plain name"),
    ],
)
def test_markdown_refuses_what_it_does_not_support(source: str, message: str) -> None:
    with pytest.raises(blog.BlogError, match=re.escape(message)):
        blog.render_markdown(source)


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def test_a_post_is_read_from_its_front_matter() -> None:
    text = (
        "---\n"
        'title: "Quoted: a title"\n'
        f"description: {DESCRIPTION}\n"
        "date: 2026-05-01\n"
        "slug: a-post\n"
        "updated: 2026-05-09\n"
        "tags:\n  - relay\n  - Claude Code\n"
        "docs: [guides/relay, /getting-started/docker/]\n"
        "draft: true\n"
        "---\n\nBody.\n"
    )
    post = blog.parse_post(text, "2026-05-01-a-post.md")
    assert (post.slug, post.title, post.date, post.updated) == (
        "a-post",
        "Quoted: a title",
        dt.date(2026, 5, 1),
        dt.date(2026, 5, 9),
    )
    assert post.tags == ("relay", "Claude Code") and post.docs == ("guides/relay", "getting-started/docker")
    assert post.draft is True and post.author == "Remembra team" and post.lede == DESCRIPTION and post.body == "Body."
    assert post.url == "https://remembra.dev/blog/a-post"
    # `updated` shows once its day has come, not before.
    assert post.modified(dt.date(2026, 5, 8)) == dt.date(2026, 5, 1)
    assert post.modified(dt.date(2026, 5, 9)) == dt.date(2026, 5, 9)


@pytest.mark.parametrize(
    ("name", "text", "message"),
    [
        ("2026-05-01-a.md", _source().replace("title: A fixture post\n", ""), "needs a title"),
        ("2026-05-01-a.md", _source(title=""), "needs a title"),
        ("2026-05-01-a.md", _source(description="x" * 49), "49 characters"),
        ("2026-05-01-a.md", _source(description="x" * 161), "161 characters"),
        ("2026-05-01-a.md", _source(date="2026-13-01"), "not a real date"),
        ("2026-05-01-a.md", _source(date="May 1, 2026"), "YYYY-MM-DD"),
        ("2026-05-01-a.md", _source().replace("date: 2026-05-01\n", ""), "date must be a date"),
        ("2026-05-02-a.md", _source(), "does not match the date in the file name"),
        ("2026-05-01-a.md", _source(extra="slug: b"), "must match the file name"),
        ("2026-05-01-index.md", _source(), "taken by the blog itself"),
        ("2026-05-01-a.md", _source(extra="updated: 2026-04-30"), "is before date"),
        ("2026-05-01-a.md", _source(extra="drafts: true"), "unknown front matter key 'drafts'"),
        ("2026-05-01-a.md", _source(extra="draft: yes"), "draft must be true or false"),
        ("2026-05-01-a.md", _source(extra="title: again"), "given twice"),
        ("2026-05-01-a.md", _source().replace("tags: [relay, handoffs]\n", ""), "1 to 8 different tags"),
        ("2026-05-01-a.md", _source().replace("[relay, handoffs]", "[relay, <b>]"), "tag '<b>'"),
        ("2026-05-01-a.md", _source(body=""), "has no body"),
        ("2026-05-01-a.md", "No front matter.\n", "must start with a front matter block"),
        ("2026-05-01-a.md", "---\ntitle: x\n", "never closed"),
        ("May-1-A-Post.md", _source(), "post files are named"),
    ],
)
def test_front_matter_mistakes_are_refused(name: str, text: str, message: str) -> None:
    with pytest.raises(blog.BlogError, match=re.escape(message)) as err:
        blog.parse_post(text, name)
    assert name in str(err.value)


def test_two_posts_cannot_share_a_slug_or_take_a_legacy_one(site: Any) -> None:
    _add(site, "2026-05-01-same-slug.md")
    _add(site, "2026-05-02-same-slug.md")
    with pytest.raises(blog.BlogError, match="slug 'same-slug' is already used by 2026-05-01-same-slug.md"):
        blog.build(site, BUILD_DAY)
    (site.content / "2026-05-02-same-slug.md").unlink()
    _add(site, "2026-05-03-why-your-ai-forgets-everything.md")
    with pytest.raises(blog.BlogError, match="already used by the legacy post"):
        blog.build(site, BUILD_DAY)
    assert blog.main(["--today", "2026-06-01"], site) == 2
    assert not site.build.exists() and not site.feed.exists()  # a refused build writes nothing


def test_a_stray_file_in_the_content_or_blog_folder_is_refused(site: Any) -> None:
    (site.content / "notes.md").write_text("not a post\n")
    with pytest.raises(blog.BlogError, match="post files are named"):
        blog.build(site, BUILD_DAY)
    (site.content / "notes.md").unlink()
    (site.content / "README.md").write_text("# How to post\n")  # the author guide is not a post
    (site.blog / "hand-written.html").write_text("<html></html>")
    with pytest.raises(blog.BlogError, match="not listed in content/blog/legacy.json"):
        blog.build(site, BUILD_DAY)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def test_future_dated_and_draft_posts_are_not_emitted_until_their_day(site: Any) -> None:
    _add(site, "2026-05-01-out-now.md", title="Out now")
    _add(site, "2026-06-10-next-week.md", title="Next week")
    _add(site, "2026-05-02-not-ready.md", title="Not ready", extra="draft: true")
    report = blog.write(site, BUILD_DAY)
    assert [p["slug"] for p in report["published"]] == ["out-now"]
    assert report["pr_title"] == "Publish: Out now"
    assert (site.blog / "out-now.html").is_file()
    for slug in ("next-week", "not-ready"):
        assert not (site.blog / f"{slug}.html").exists()
        for output in (site.index, site.feed, site.sitemap):
            assert slug not in output.read_text(), (slug, output.name)
    assert blog.pending(site, BUILD_DAY) == [
        "2026-05-02  draft      not-ready  Not ready",
        "2026-06-10  scheduled  next-week  Next week",
    ]
    # The day before it is still held back; on its day it is published.
    assert blog.write(site, dt.date(2026, 6, 9))["changed"] is False
    report = blog.write(site, dt.date(2026, 6, 10))
    assert report["pr_title"] == "Publish: Next week" and report["written"] == [
        "landing/blog/index.html",
        "landing/blog/next-week.html",
        "landing/feed.xml",
        "landing/sitemap.xml",
    ]
    assert "/blog/next-week" in site.index.read_text() and "/blog/next-week" in site.sitemap.read_text()
    assert blog.pending(site, dt.date(2026, 6, 10)) == ["2026-05-02  draft      not-ready  Not ready"]


def test_check_uses_the_recorded_build_date_not_the_clock(site: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The committed output is checked as of build.json's date. Both fixture dates are long past on any real clock,
    so a check that read the clock would call the tree stale (the 2026-01-10 post would be due)."""
    _add(site, "2026-01-05-early.md")
    _add(site, "2026-01-10-later.md")
    assert blog.main(["--today", "2026-01-06"], site) == 0
    assert json.loads(site.build.read_text()) == {"built_through": "2026-01-06"}

    class _NoClock(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            raise AssertionError("--check must not read the clock")

    monkeypatch.setattr(blog.dt, "datetime", _NoClock)
    assert blog.check(site) == []
    assert blog.main(["--check"], site) == 0
    assert not (site.blog / "later.html").exists()


def test_a_run_that_changes_nothing_leaves_the_tree_alone(site: Any) -> None:
    _add(site, "2026-05-01-out-now.md")
    _add(site, "2026-07-01-scheduled.md")
    blog.write(site, BUILD_DAY)
    before = {p: p.read_bytes() for p in sorted(site.landing.parent.rglob("*")) if p.is_file()}
    report = blog.write(site, dt.date(2026, 6, 20))  # a later day on which nothing comes due
    assert report["changed"] is False and report["built_through"] == "2026-06-01" and report["published"] == []
    assert {p: p.read_bytes() for p in sorted(site.landing.parent.rglob("*")) if p.is_file()} == before
    assert blog.check(site) == []


def test_check_fails_after_a_source_change_and_passes_again_after_a_rebuild(
    site: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _add(site, "2026-05-01-out-now.md", title="Out now")
    assert blog.main(["--today", "2026-06-01"], site) == 0
    assert blog.main(["--check"], site) == 0
    source.write_text(source.read_text().replace("More words.", "More words, edited."))
    capsys.readouterr()
    assert blog.main(["--check"], site) == 1
    assert "landing/blog/out-now.html is stale" in capsys.readouterr().out
    assert blog.main(["--today", "2026-06-01"], site) == 0
    assert blog.main(["--check"], site) == 0
    # A generated page edited by hand is stale too.
    page = site.blog / "out-now.html"
    page.write_text(page.read_text().replace("edited", "hand-edited"))
    assert blog.check(site) == ["landing/blog/out-now.html is stale"]


def test_an_unpublished_post_loses_its_page(site: Any) -> None:
    source = _add(site, "2026-05-01-out-now.md")
    blog.write(site, BUILD_DAY)
    source.write_text(source.read_text().replace("tags:", "draft: true\ntags:"))
    assert blog.check(site) == [
        "landing/blog/index.html is stale",
        "landing/feed.xml is stale",
        "landing/sitemap.xml is stale",
        "landing/blog/out-now.html has no published post",
    ]
    report = blog.write(site, BUILD_DAY)
    assert report["removed"] == ["landing/blog/out-now.html"] and not (site.blog / "out-now.html").exists()
    assert "out-now" not in site.sitemap.read_text() and blog.check(site) == []
    for slug in LEGACY:  # hand-written pages are never removed
        assert (site.blog / f"{slug}.html").read_bytes() == (LANDING / "blog" / f"{slug}.html").read_bytes()


# ---------------------------------------------------------------------------
# The generated pages
# ---------------------------------------------------------------------------


def test_a_generated_post_is_a_complete_page(site: Any) -> None:
    _add(
        site,
        "2026-05-01-handoffs-that-work.md",
        title='Handoffs <that> "work" & more',
        body="Intro words here.\n\n## The setup\n\nSee [below](#the-setup) and [the guide](https://docs.remembra.dev/guides/relay/#x).\n",
        extra="updated: 2026-05-20\ndocs: [guides/relay]",
    )
    blog.write(site, BUILD_DAY)
    page = (site.blog / "handoffs-that-work.html").read_text()
    assert partials.render(page) == page  # the shared head, header and footer are exact
    assert "blog/handoffs-that-work.html" not in partials.PAGES  # generated pages are checked here, not by PAGES
    assert len(re.findall(r"<h1\b", page)) == 1
    assert '<h1 id="post-title">Handoffs &lt;that&gt; "work" &amp; more</h1>' in page
    assert '<title>Handoffs &lt;that&gt; "work" &amp; more · Remembra</title>' in page
    assert page.count('<link rel="canonical" href="https://remembra.dev/blog/handoffs-that-work">') == 1
    assert f'<meta name="description" content="{DESCRIPTION}">' in page
    for needed in (
        '<meta property="og:type" content="article">',
        '<meta property="og:url" content="https://remembra.dev/blog/handoffs-that-work">',
        '<meta property="og:title" content="Handoffs &lt;that&gt; &quot;work&quot; &amp; more">',
        '<meta property="og:image" content="https://remembra.dev/social-preview.png">',
        '<meta property="og:image:alt" content="',
        '<meta property="article:published_time" content="2026-05-01">',
        '<meta property="article:modified_time" content="2026-05-20">',
        '<meta property="article:tag" content="relay">',
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="twitter:title" content="',
        '<link rel="alternate" type="application/rss+xml" title="Remembra blog" href="/feed.xml">',
        '<body class="site" data-page="blog">',
        '<p class="eyebrow"><a href="/blog">Blog</a> · Updated <time datetime="2026-05-20">May 20, 2026</time></p>',
        f'<p class="lede">{DESCRIPTION}</p>',
        '<article class="prose">',
        '<script src="/site.js" defer></script>',
        '<h2 id="the-setup">The setup</h2>',
        '<li><a href="https://docs.remembra.dev/guides/relay/">Remembra Relay</a></li>',
    ):
        assert needed in page, needed
    assert (LANDING / "social-preview.png").is_file()
    assert not re.search(r"\$(49|199)\b", page)
    posting, crumbs = _ld(page)
    assert posting["@type"] == "BlogPosting" and posting["@context"] == "https://schema.org"
    assert posting["headline"] == 'Handoffs <that> "work" & more' and posting["description"] == DESCRIPTION
    assert (posting["datePublished"], posting["dateModified"]) == ("2026-05-01", "2026-05-20")
    assert posting["author"] == {"@type": "Organization", "name": "Remembra", "url": "https://remembra.dev"}
    assert posting["publisher"]["logo"]["url"] == "https://remembra.dev/logo.jpg" and (LANDING / "logo.jpg").is_file()
    assert posting["mainEntityOfPage"]["@id"] == "https://remembra.dev/blog/handoffs-that-work"
    assert posting["image"] == "https://remembra.dev/social-preview.png"
    assert posting["keywords"] == "relay, handoffs" and posting["wordCount"] >= 10
    assert crumbs["@type"] == "BreadcrumbList"
    assert [(c["position"], c["item"]) for c in crumbs["itemListElement"]] == [
        (1, "https://remembra.dev/"),
        (2, "https://remembra.dev/blog"),
        (3, "https://remembra.dev/blog/handoffs-that-work"),
    ]
    # The only executable inline script is the shared theme bootstrap, which the CSP already hashes.
    flags = re.S | re.I  # tags are matched in any case, with or without attributes on the closing tag
    scripts = re.findall(r"<script(?![^>]*\b(?:src|type)=)[^>]*>(.*?)</script\b[^>]*>", page, flags)
    assert scripts == re.findall(r"<script\b[^>]*>(.*?)</script\b[^>]*>", partials.HEAD, flags)
    assert not re.search(r"\son[a-z]+=", page)


def test_json_ld_cannot_end_its_own_script_tag(site: Any) -> None:
    _add(site, "2026-05-01-tricky.md", title="</script><script>alert(1)</script> & <!-- @lockup -->")
    blog.write(site, BUILD_DAY)
    page = (site.blog / "tricky.html").read_text()
    assert "alert(1)</script>" not in page and "<!-- @lockup -->" not in page
    assert len(re.findall(r"<script\b", page)) == 4  # theme bootstrap, two JSON-LD blocks, site.js
    assert _ld(page)[0]["headline"] == "</script><script>alert(1)</script> & <!-- @lockup -->"
    listed = [p["headline"] for p in _ld(site.index.read_text())[0]["blogPost"]]
    assert "</script><script>alert(1)</script> & <!-- @lockup -->" in listed
    assert partials.render(site.index.read_text()) == site.index.read_text()
    assert partials.render(page) == page


def test_a_dead_docs_link_fails_the_build(site: Any) -> None:
    good = _add(site, "2026-05-01-linked.md", extra="docs: [guides/relay, getting-started/docker]")
    assert blog.main(["--today", "2026-06-01"], site) == 0
    good.write_text(_source(extra="docs: [guides/no-such-page]"))
    with pytest.raises(blog.BlogError, match="docs path 'guides/no-such-page' is not a page mkdocs.yml builds"):
        blog.build(site, BUILD_DAY)
    assert blog.main(["--today", "2026-06-01"], site) == 2 and blog.main(["--check"], site) == 2
    good.write_text(_source(body="Read [this](https://docs.remembra.dev/guides/no-such-page/#part)."))
    with pytest.raises(blog.BlogError, match="https://docs.remembra.dev/guides/no-such-page/#part is not a page"):
        blog.build(site, BUILD_DAY)
    good.write_text(_source(extra="docs: [../secrets]"))
    with pytest.raises(blog.BlogError, match="is not a page mkdocs.yml builds"):
        blog.build(site, BUILD_DAY)
    good.write_text(_source(body="Jump to [nowhere](#missing)."))
    with pytest.raises(blog.BlogError, match="#missing matches no heading"):
        blog.build(site, BUILD_DAY)
    good.write_text(
        _source(body="Read [the docs](https://docs.remembra.dev/) and [relay](https://docs.remembra.dev/guides/relay/).")
    )
    assert blog.main(["--today", "2026-06-01"], site) == 0


def test_the_index_lists_every_post_newest_first(site: Any) -> None:
    _add(site, "2026-05-01-older.md", title="Older post")
    _add(site, "2026-10-02-newer.md", title="Newer & better")
    blog.write(site, dt.date(2026, 11, 1))
    page = site.index.read_text()
    assert partials.render(page) == page and "blog/index.html" in partials.PAGES
    order = re.findall(r'<h2><a href="/blog/([a-z0-9-]+)">', page)
    assert order == [
        "newer",
        "remembra-vs-mem0-vs-zep",  # revised 2026-09-25
        "older",
        "building-remembra-with-ai-agents",
        "multi-agent-orchestration-patterns",
        "why-your-ai-forgets-everything",
    ]
    assert '<h2><a href="/blog/newer">Newer &amp; better</a></h2>' in page
    assert '<p class="when"><time datetime="2026-10-02">October 2, 2026</time></p>' in page
    assert '<p class="when">Updated <time datetime="2026-09-25">September 25, 2026</time></p>' in page
    assert '<ol class="posts">' in page and len(re.findall(r"<h1\b", page)) == 1
    assert '<link rel="alternate" type="application/rss+xml" title="Remembra blog" href="/feed.xml">' in page
    assert '<link rel="canonical" href="https://remembra.dev/blog">' in page
    assert '<meta property="og:url" content="https://remembra.dev/blog">' in page
    assert '<meta name="twitter:card" content="summary_large_image">' in page
    (listing,) = _ld(page)
    assert listing["@type"] == "Blog" and listing["url"] == "https://remembra.dev/blog"
    assert [p["url"].rsplit("/", 1)[1] for p in listing["blogPost"]] == order
    assert listing["blogPost"][1]["datePublished"] == "2026-03-08" and listing["blogPost"][1]["dateModified"] == "2026-09-25"


# ---------------------------------------------------------------------------
# Feed and sitemap
# ---------------------------------------------------------------------------


def test_the_feed_is_well_formed_rss_with_stable_guids(site: Any) -> None:
    _add(site, "2026-05-01-first-post.md", title="First <post> & more")
    blog.write(site, BUILD_DAY)
    root = ET.fromstring(site.feed.read_text())
    assert root.tag == "rss" and root.attrib["version"] == "2.0"
    channel = root.find("channel")
    assert channel is not None
    assert channel.findtext("link") == "https://remembra.dev/blog" and channel.findtext("title") == "Remembra blog"
    me = channel.find("{http://www.w3.org/2005/Atom}link")
    assert me is not None and me.attrib == {
        "href": "https://remembra.dev/feed.xml",
        "rel": "self",
        "type": "application/rss+xml",
    }
    items = channel.findall("item")
    assert len(items) == 5
    assert [i.findtext("guid") for i in items] == [
        "https://remembra.dev/blog/remembra-vs-mem0-vs-zep",
        "https://remembra.dev/blog/first-post",
        "https://remembra.dev/blog/building-remembra-with-ai-agents",
        "https://remembra.dev/blog/multi-agent-orchestration-patterns",
        "https://remembra.dev/blog/why-your-ai-forgets-everything",
    ]
    post = items[1]
    assert post.findtext("title") == "First <post> & more" and post.findtext("description") == DESCRIPTION
    assert post.findtext("link") == post.findtext("guid") and post.find("guid").attrib == {"isPermaLink": "true"}
    assert post.findtext("pubDate") == "Fri, 01 May 2026 00:00:00 GMT"
    assert [c.text for c in post.findall("category")] == ["relay", "handoffs"]
    assert channel.findtext("lastBuildDate") == "Fri, 25 Sep 2026 00:00:00 GMT"  # the newest change, not the build day
    rfc822 = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} [A-Z][a-z]{2} \d{4} 00:00:00 GMT$")
    assert all(rfc822.match(i.findtext("pubDate") or "") for i in items)


def test_the_feed_holds_the_latest_twenty_posts(site: Any) -> None:
    for day in range(1, 26):
        _add(site, f"2026-10-{day:02d}-post-{day}.md", title=f"Post {day}")
    blog.write(site, dt.date(2026, 11, 1))
    guids = [i.findtext("guid") for i in ET.fromstring(site.feed.read_text()).iter("item")]
    assert len(guids) == 20 and len(set(guids)) == 20
    assert guids[0] == "https://remembra.dev/blog/post-25" and guids[-1] == "https://remembra.dev/blog/post-6"
    assert len(re.findall(r'<h2><a href="/blog/', site.index.read_text())) == 29  # the index still lists all of them


def test_only_the_blog_lines_of_the_sitemap_change(site: Any) -> None:
    before = site.sitemap.read_text()
    _add(site, "2026-05-01-first-post.md", extra="updated: 2026-05-20")
    _add(site, "2026-07-01-scheduled.md")
    blog.write(site, BUILD_DAY)
    after = site.sitemap.read_text()

    def others(xml: str) -> list[str]:
        return [line for line in xml.splitlines(keepends=True) if "<loc>https://remembra.dev/blog" not in line]

    assert others(after) == others(before)  # byte for byte, in order
    crew = (
        "  <!-- @crew-url --><url><loc>https://remembra.dev/crew</loc>"
        "<changefreq>monthly</changefreq><priority>0.8</priority></url><!-- /@crew-url -->\n"
    )
    assert crew in after and partials.render_sitemap(after) == after
    blog_lines = [line for line in after.splitlines() if "<loc>https://remembra.dev/blog" in line]
    assert blog_lines == [
        "  <url><loc>https://remembra.dev/blog</loc><lastmod>2026-09-25</lastmod><changefreq>monthly</changefreq><priority>0.4</priority></url>",
        "  <url><loc>https://remembra.dev/blog/remembra-vs-mem0-vs-zep</loc><lastmod>2026-09-25</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
        "  <url><loc>https://remembra.dev/blog/first-post</loc><lastmod>2026-05-20</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>",
        "  <url><loc>https://remembra.dev/blog/building-remembra-with-ai-agents</loc><lastmod>2026-03-15</lastmod><changefreq>yearly</changefreq><priority>0.3</priority></url>",
        "  <url><loc>https://remembra.dev/blog/multi-agent-orchestration-patterns</loc><lastmod>2026-03-14</lastmod><changefreq>yearly</changefreq><priority>0.3</priority></url>",
        "  <url><loc>https://remembra.dev/blog/why-your-ai-forgets-everything</loc><lastmod>2026-03-07</lastmod><changefreq>yearly</changefreq><priority>0.3</priority></url>",
    ]
    assert "feed.xml" not in after  # the sitemap lists pages; the feed is found from the pages' <link rel="alternate">
    ET.fromstring(after)
    # With no posts of its own the script leaves the sitemap exactly as it is.
    assert blog.render_sitemap(before, blog.ordered(blog.load_legacy(site), BUILD_DAY), BUILD_DAY) == before


def test_a_sitemap_with_no_blog_lines_gets_them_before_the_closing_tag() -> None:
    xml = '<?xml version="1.0"?>\n<urlset>\n  <url><loc>https://remembra.dev/</loc></url>\n</urlset>\n'
    post = blog.Post(slug="a", title="A", description=DESCRIPTION, date=dt.date(2026, 5, 1))
    assert blog.render_sitemap(xml, [post], BUILD_DAY) == (
        '<?xml version="1.0"?>\n<urlset>\n  <url><loc>https://remembra.dev/</loc></url>\n'
        "  <url><loc>https://remembra.dev/blog</loc><lastmod>2026-05-01</lastmod><changefreq>monthly</changefreq><priority>0.4</priority></url>\n"
        "  <url><loc>https://remembra.dev/blog/a</loc><lastmod>2026-05-01</lastmod><changefreq>monthly</changefreq><priority>0.5</priority></url>\n"
        "</urlset>\n"
    )


# ---------------------------------------------------------------------------
# Command line, preview and the publish workflow
# ---------------------------------------------------------------------------


def test_the_report_names_what_the_publish_pull_request_is_called(site: Any, tmp_path: Path) -> None:
    _add(site, "2026-05-01-one.md", title="One")
    _add(site, "2026-05-02-two.md", title="Two")
    report_file = tmp_path / "report.json"
    assert blog.main(["--today", "2026-06-01", "--report", str(report_file)], site) == 0
    report = json.loads(report_file.read_text())
    assert report["changed"] is True and report["pr_title"] == "Publish: Two, One" and report["built_through"] == "2026-06-01"
    assert blog.main(["--today", "2026-06-02", "--report", str(report_file)], site) == 0
    assert json.loads(report_file.read_text())["changed"] is False
    assert blog.main(["--today", "June 2"], site) == 2


def test_pending_prints_scheduled_posts_and_drafts(site: Any, capsys: pytest.CaptureFixture[str]) -> None:
    _add(site, "2026-05-01-out-now.md")
    _add(site, "2026-07-01-scheduled.md", title="Scheduled one")
    assert blog.main(["--today", "2026-06-01"], site) == 0
    capsys.readouterr()
    assert blog.main(["--pending"], site) == 0  # as of the recorded build date
    assert capsys.readouterr().out == "2026-07-01  scheduled  scheduled  Scheduled one\n"
    assert blog.main(["--pending", "--today", "2026-07-01"], site) == 0
    assert capsys.readouterr().out == "nothing pending (built through 2026-07-01)\n"


def test_preview_renders_drafts_and_resolves_clean_urls(site: Any) -> None:
    _add(site, "2026-05-01-out-now.md")
    _add(site, "2027-01-01-far-off.md", extra="draft: true\nupdated: 2027-02-01")
    outputs = blog.build(site, BUILD_DAY, preview=True)
    assert site.blog / "far-off.html" in outputs and "/blog/far-off" in outputs[site.index]
    assert site.blog / "far-off.html" not in blog.build(site, BUILD_DAY)
    blog.write(site, BUILD_DAY)
    root = site.landing
    assert blog.clean_url_file(root, "/blog") == (site.blog / "index.html").resolve()
    assert blog.clean_url_file(root, "/blog/out-now?x=1") == (site.blog / "out-now.html").resolve()
    assert blog.clean_url_file(root, "/feed.xml") == site.feed.resolve()
    assert blog.clean_url_file(root, "/blog/missing") is None
    assert blog.clean_url_file(root, "/../content/blog/legacy.json") is None  # nothing outside the web root


def test_the_publish_workflow_opens_a_pull_request_and_never_touches_main() -> None:
    text = (WORKFLOWS / "blog-publish.yml").read_text()
    ci = (WORKFLOWS / "ci.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses  # pinned by full commit SHA
    for action in ("actions/checkout", "actions/setup-python"):
        pinned = re.search(rf"uses:\s*({re.escape(action)}@[0-9a-f]{{40}})", ci)
        assert pinned is not None and pinned.group(1) in uses, action  # the same pins CI uses
    assert re.search(r"^  schedule:\n    - cron: ", text, re.M) and re.search(r"^  workflow_dispatch:", text, re.M)
    assert "pull_request" not in text.split("jobs:")[0]  # never triggered by a pull request's code
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)  # the default; the job widens it
    assert re.search(r"^    permissions:\n      contents: write\n      pull-requests: write\n", text, re.M)
    assert "blog/publish-" in text and "gh pr create" in text and "scripts/site_blog.py --check" in text
    runs = "\n".join(re.findall(r"^\s+run: \|\n((?:\s{10,}.*\n|\n)+)", text, re.M))
    assert "gh pr create" in runs
    assert "${{" not in runs  # nothing is interpolated into a shell script; values arrive through env
    assert not re.search(r"gh pr merge|--auto|git push[^\n]*\b(main|HEAD:main)\b|--force", text)
    assert re.search(r'git push origin "HEAD:refs/heads/\$\{?branch\}?"', runs)


# ---------------------------------------------------------------------------
# The committed tree
# ---------------------------------------------------------------------------


def test_the_committed_blog_is_in_sync_with_its_sources(capsys: pytest.CaptureFixture[str]) -> None:
    assert blog.check(blog.DEFAULT_PATHS) == [], "run python scripts/site_blog.py"
    assert blog.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out
    through = blog.built_through(blog.DEFAULT_PATHS)
    assert through is not None
    outputs = blog.build(blog.DEFAULT_PATHS, through)
    assert (LANDING / "feed.xml").read_text() == outputs[LANDING / "feed.xml"]
    assert (LANDING / "blog" / "index.html").read_text() == outputs[LANDING / "blog" / "index.html"]
    assert (LANDING / "sitemap.xml").read_text() == outputs[LANDING / "sitemap.xml"]


def test_the_committed_blog_lists_the_legacy_posts_and_generated_pages_stay_out_of_pages() -> None:
    manifest = json.loads((CONTENT / "legacy.json").read_text())
    assert sorted(row["slug"] for row in manifest) == sorted(LEGACY)
    index = (LANDING / "blog" / "index.html").read_text()
    feed = ET.fromstring((LANDING / "feed.xml").read_text())
    guids = [i.findtext("guid") for i in feed.iter("item")]
    assert 4 <= len(guids) <= 20 and len(set(guids)) == len(guids)
    listed = re.findall(r'<h2><a href="/blog/([a-z0-9-]+)">', index)
    for slug in LEGACY:
        assert slug in listed and f"blog/{slug}.html" in partials.PAGES
        assert blog.MARKER not in (LANDING / "blog" / f"{slug}.html").read_text()  # hand-written, never regenerated
    for page in sorted((LANDING / "blog").glob("*.html")):
        text = page.read_text()
        assert page.stem == "index" or page.stem in listed, page.name
        if blog.MARKER in text and page.name != "index.html":
            assert f"blog/{page.name}" not in partials.PAGES
            assert partials.render(text) == text, page.name
    assert "blog/index.html" in partials.PAGES and "feed.xml" not in (LANDING / "sitemap.xml").read_text()
    # Post sources live outside landing/, which is published whole.
    assert not list(LANDING.rglob("*.md")) or all(p.name == "setup.md" for p in LANDING.rglob("*.md"))
    assert LANDING not in CONTENT.parents


def test_every_page_links_the_blog_and_announces_the_feed() -> None:
    for name in partials.PAGES:
        page = (LANDING / name).read_text()
        footer = page[page.index("<!-- @footer -->") :]
        assert '<a href="/blog"' in footer, name
        assert '<link rel="alternate" type="application/rss+xml" title="Remembra blog" href="/feed.xml">' in page, name
    assert (LANDING / "feed.xml").is_file()
