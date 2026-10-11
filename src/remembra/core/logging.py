"""Structured logging configuration (structlog)."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# Keys that could carry what a user asked the Marshal desk or what it read for them.
# Events of the desk (component="marshal_desk") never keep them, whatever a caller passes.
MARSHAL_DROPPED_FIELDS = frozenset(
    {
        "content",
        "question",
        "messages",
        "body",
        "text",
        "query",
        "answer",
        "prompt",
        "arguments",
        "result",
        "history",
        "detail",
        "summary",
    }
)


class _NoProviderRequestOptions(logging.Filter):
    """SDK debug request options contain prompts and tools, including private reads."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("Request options:")


def drop_marshal_content(logger: Any, name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """structlog processor: remove :data:`MARSHAL_DROPPED_FIELDS` from Marshal desk events (metadata only)."""
    if event_dict.get("component") == "marshal_desk":
        for key in MARSHAL_DROPPED_FIELDS.intersection(event_dict):
            event_dict.pop(key, None)
    return event_dict


def configure_logging(log_level: str = "info") -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    sdk_logger = logging.getLogger("openai._base_client")
    if not any(isinstance(f, _NoProviderRequestOptions) for f in sdk_logger.filters):
        sdk_logger.addFilter(_NoProviderRequestOptions())
    # This driver's DEBUG messages contain SQL parameters, including private text.
    logging.getLogger("aiosqlite").setLevel(logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            drop_marshal_content,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if sys.stderr.isatty() else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
