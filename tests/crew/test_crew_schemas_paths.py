"""``claim_covers_path``: which reserved path claims an adopt by path takes."""

import pytest

from remembra.crew.gatecore import claim_covers_path


@pytest.mark.parametrize(
    ("path", "glob", "hit"),
    [
        ("art/lls/cover.psd", "art/lls/cover.psd", True),
        ("art/lls", "art/lls/cover.psd", True),  # a directory names the claims under it
        ("art/lls/", "art/lls/cover.psd", True),
        ("art/lls/cover.psd", "art/lls/**", True),
        ("art/lls/cover.psd", "art/*/cover.psd", True),
        ("art/lls", "art/lls/**", True),
        ("art/llsx/cover.psd", "art/lls/**", False),
        ("art/x/y.txt", "art/*", False),  # crew globs: * stays within one segment (Codex review P2)
        ("art/x", "art/*", True),
        ("art/ll", "art/lls/cover.psd", False),
        ("art/lls/cover.psd", "", False),
        ("", "art/lls/cover.psd", False),
    ],
)
def test_claim_covers_path(path, glob, hit):
    assert claim_covers_path(path, glob) is hit
