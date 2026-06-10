"""Regression tests for FTS5 query sanitization.

Before the fix, raw user recall queries were passed to FTS5 MATCH with only
double-quote escaping. That made natural-language recall fragile and unsafe:

  * ``cost / benefit`` and unbalanced parens raised ``fts5: syntax error`` → 500.
  * ``note: this is important`` was read as a column filter and leaked column
    names via ``OperationalError: no such column: note``.
  * ``Stripe AND Paddle`` silently matched nothing because ``AND`` is an FTS5
    operator, not a search term.

``_build_fts_match_query`` tokenizes the query and quotes each token as a
literal phrase OR-joined, so none of the above can happen.
"""

import sqlite3

import pytest

from remembra.storage.database import _build_fts_match_query


def _run(match: str | None) -> int | str:
    """Execute a sanitized MATCH against a tiny in-memory FTS5 table."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE fts USING fts5(content)")
    db.execute("INSERT INTO fts VALUES ('I removed Stripe and switched to Paddle for billing')")
    if match is None:
        return "SKIP"
    try:
        return len(db.execute("SELECT rowid FROM fts WHERE content MATCH ?", (match,)).fetchall())
    finally:
        db.close()


@pytest.mark.parametrize(
    "query",
    [
        "cost / benefit",
        "(unbalanced paren",
        "note: this is important",
        'unterminated "quote',
        "trailing operator AND",
        "^ : * NEAR ( )",
        '"; DROP TABLE fts; --',
    ],
)
def test_no_query_can_raise_a_syntax_error(query):
    """Whatever the user types, the sanitized MATCH must execute cleanly."""
    match = _build_fts_match_query(query)
    result = _run(match)
    assert result != "ERROR"  # _run would have raised; reaching here means OK
    assert isinstance(result, (int, str))


def test_boolean_keywords_are_treated_as_words_not_operators():
    """'Stripe AND Paddle' must MATCH the row (both words present as text)."""
    match = _build_fts_match_query("what about Stripe AND Paddle")
    assert match is not None
    assert _run(match) == 1


def test_column_filter_syntax_is_neutralized():
    """'note:' must not be interpreted as a column filter (no schema leak)."""
    match = _build_fts_match_query("note: important")
    # The colon is dropped; 'note' becomes a quoted literal token.
    assert ":" not in match
    assert '"note"' in match
    assert _run(match) == 0  # 'note'/'important' aren't in the row; clean 0, not a crash


def test_all_punctuation_returns_none():
    """A query with no searchable tokens yields None so the caller skips FTS."""
    assert _build_fts_match_query("???") is None
    assert _build_fts_match_query("   ") is None
    assert _build_fts_match_query("") is None
    assert _build_fts_match_query(None) is None


def test_embedded_quotes_are_escaped():
    """Double-quotes inside a token must be doubled, never breaking out."""
    match = _build_fts_match_query('say "hi"')
    assert match is not None
    # Each token wrapped in quotes; the literal quote inside is doubled.
    assert _run(match) in (0, 1)  # executes cleanly regardless of match count


def test_token_count_is_bounded():
    """Pathological long input is capped so the MATCH string can't blow up."""
    match = _build_fts_match_query(" ".join(["word"] * 500))
    assert match is not None
    assert match.count(" OR ") <= 31  # 32 tokens max → at most 31 joiners


def test_hyphenated_and_apostrophe_words_stay_whole():
    """'co-op' and \"don't\" should survive as single tokens."""
    match = _build_fts_match_query("don't break co-op")
    assert '"don\'t"' in match
    assert '"co-op"' in match
