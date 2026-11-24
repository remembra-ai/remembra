# Writing for the Remembra blog

Posts are Markdown files here. `scripts/site_blog.py` turns them into `landing/blog/` pages, the
index, `landing/feed.xml` and the blog lines of `landing/sitemap.xml`; that output is committed.

## Add a post

1. Create `content/blog/<YYYY-MM-DD>-<slug>.md` (lowercase letters, digits, hyphens):

   ```markdown
   ---
   title: What a handoff carries
   description: One or two sentences, 50 to 160 characters. Shown in search results, link previews and the feed.
   date: 2027-01-12
   tags: [relay, handoffs]
   docs: [guides/relay]        # optional: "Related docs" links, checked against mkdocs.yml
   updated: 2027-02-01         # optional: shows "Updated ..." and moves the post up the list
   author: Remembra team       # optional, this is the default
   lede: A longer opening line # optional, defaults to the description
   draft: true                 # optional: never published while set
   ---

   Body in Markdown.
   ```

2. Run `python scripts/site_blog.py`, then `python scripts/site_blog.py --check`.
3. Commit the source and everything the script wrote, and open one pull request per post.

Markdown that works: `##` to `####` headings (each gets an `#anchor`), paragraphs, fenced code
with a language, `inline code`, **bold**, *italic*, links, `![alt](/path-on-this-site.png)`
images, one-level lists, blockquotes and pipe tables. Raw HTML is shown as text, not run. Links
are `https://`, `mailto:`, a site path (`/pricing`) or `#anchor`; a docs.remembra.dev link that
mkdocs.yml does not build fails the build.

## Schedule a post

Give it a future `date`. The build publishes a post only when its date has come and it is not
a draft, so the pull request that adds a scheduled post contains only the source file.
`python scripts/site_blog.py --pending` lists what is waiting. Each day a workflow rebuilds with
that day's date (UTC); when a post comes due it opens a pull request titled "Publish: <title>".

## Preview

`python scripts/site_blog.py --serve` builds every post, drafts and future dates included, into
a temporary copy of the site and serves it with the site's clean URLs at
http://127.0.0.1:8080/blog. It changes nothing in the repository. (`python -m http.server`
will not do: the site links `/blog/<slug>` with no `.html`.) For the real nginx:
`docker build -t remembra-site landing && docker run --rm -p 8080:8080 remembra-site`.

## Approval

Merging is the approval. One pull request per post; nothing reaches remembra.dev until its
generated page is on `main` and the site is deployed. A post dated today or earlier goes live
with its own pull request. A future-dated post is approved twice: once as a source file, and
again when the daily "Publish:" pull request is merged.

The tests (`tests/test_site_blog.py` and the landing tests) hold every page to the site's rules
and the committed output to the sources as of the date in `build.json`, never the clock. The
four posts written by hand before this pipeline are listed in `legacy.json` and stay as they are.
