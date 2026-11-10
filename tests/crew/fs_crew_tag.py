"""The crew tag: the last line of every crew write's answer, ``(in CREW <project> as <callsign>)``.

The tests of the earlier fault-survey fixes pin the text of a write's answer. Since F6 every such answer
ends with the crew it was written to, so those tests check the tag and compare the text above it.
"""

from __future__ import annotations

import re


def untagged(text: str, project: str) -> str:
    """The answer of a crew write without its last line, which must be the crew tag of ``project``."""
    body, newline, tag = text.rpartition("\n")
    assert newline and re.fullmatch(rf"\(in CREW {re.escape(project)} as [a-z][a-z0-9-]*\)", tag), text
    return body
