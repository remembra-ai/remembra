"""Prompt-safety helpers shared by extraction, consolidation and ingest (ING-16/17).

Untrusted text (user content, stored memories, transcripts) is fenced inside
``<untrusted_data>`` tags and any tag look-alikes inside it are neutralised,
so content cannot close the fence and smuggle instructions into the prompt.
"""

from __future__ import annotations

import re
from datetime import datetime

_TAG_RE = re.compile(r"<\s*/?\s*untrusted_data[^>]*>", re.IGNORECASE)


def escape_untrusted(text: str) -> str:
    """Neutralise fence tags inside untrusted text."""
    return _TAG_RE.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)


def wrap_untrusted(text: str) -> str:
    """Fence untrusted text; the model is told never to follow instructions inside."""
    return f"<untrusted_data>\n{escape_untrusted(text)}\n</untrusted_data>"


def reference_date_line(reference: datetime | None) -> str:
    """One line telling the model 'today' so relative dates can be resolved."""
    if reference is None:
        return ""
    return (
        f"REFERENCE DATE (when this text was recorded): {reference.strftime('%Y-%m-%d')} "
        f"({reference.strftime('%A')}). Resolve relative dates such as 'today', 'yesterday', "
        "'next month' or 'on Friday' to absolute dates using this reference date."
    )
