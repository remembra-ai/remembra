"""
Import from ChatGPT conversation export (JSON format).

ChatGPT export structure (conversations.json):
[
  {
    "title": "conversation title",
    "create_time": 1700000000.0,
    "mapping": {
      "<node_id>": {
        "message": {
          "author": {"role": "user" | "assistant"},
          "content": {"parts": ["text content"]},
          "create_time": 1700000000.0
        }
      }
    }
  }
]

This importer extracts user messages as individual memories
with conversation context in metadata.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime

from remembra.io.importers import ImportedMemory

logger = logging.getLogger(__name__)


def parse_chatgpt_export(data: str | bytes) -> list[ImportedMemory]:
    """Parse a ChatGPT conversations.json export.

    Args:
        data: Raw JSON string or bytes.

    Returns:
        List of ImportedMemory records (one per user message).
    """
    try:
        conversations = json.loads(data)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse ChatGPT export: %s", e)
        return []

    if not isinstance(conversations, list):
        conversations = [conversations]

    memories: list[ImportedMemory] = []

    for conv in conversations:
        title = conv.get("title", "Untitled")
        mapping = conv.get("mapping", {})

        for node_id, node in mapping.items():
            msg = node.get("message")
            if msg is None:
                continue

            author = msg.get("author", {}).get("role", "")
            if author != "user":
                continue

            content_parts = msg.get("content", {}).get("parts", [])
            text = " ".join(str(p) for p in content_parts if isinstance(p, str)).strip()
            if not text or len(text) < 10:
                continue

            create_time = msg.get("create_time")
            ts = None
            if create_time:
                with contextlib.suppress(ValueError, OSError):
                    ts = datetime.fromtimestamp(create_time, tz=UTC).isoformat()

            memories.append(
                ImportedMemory(
                    content=text,
                    metadata={
                        "conversation_title": title,
                        "original_role": "user",
                    },
                    source_format="chatgpt",
                    source_id=node_id,
                    timestamp=ts,
                )
            )

    logger.info("Parsed %d memories from ChatGPT export", len(memories))
    return memories
