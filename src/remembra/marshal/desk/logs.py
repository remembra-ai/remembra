"""The desk's logs: metadata only, never what anyone asked or read.

Every event is bound ``component="marshal_desk"``; the processor
:func:`remembra.core.logging.drop_marshal_content` removes content-bearing
keys (question, answer, messages, tool payloads...) from such events as a
second guard. There is no transcript table.
"""

from __future__ import annotations

from typing import Any

import structlog

# A lazy logger with the component as its initial value, never ``.bind()`` at import: a logger bound here
# would keep the processors in force when this module loads, before the app configures logging, and
# skip the app's chain (its JSON renderer and drop_marshal_content). This one reads the chain per event.
log = structlog.get_logger(__name__, component="marshal_desk")

# The metadata ``marshal_ask`` carries (plan 2.9); anything else passed in is dropped here.
ASK_FIELDS = (
    "user_id",
    "conv_hash",
    "turn",
    "source",
    "model",
    "model_calls",
    "tools",
    "reads_failed",
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "usd",
    "outcome",
    "fallback_reason",
    "duration_ms",
    "input_redactions",
)


def ask_log(**metadata: Any) -> None:
    log.info("marshal_ask", **{key: metadata.get(key) for key in ASK_FIELDS})
