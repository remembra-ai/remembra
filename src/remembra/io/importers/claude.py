"""
Import from Claude conversation export (JSON format).

Claude export structure (conversations.json):
[
  {
    "uuid": "...",
    "name": "conversation title",
    "created_at": "2024-01-01T00:00:00Z",
    "chat_messages": [
      {
        "uuid": "...",
        "sender": "human" | "assistant",
        "text": "message content",
        "created_at": "2024-01-01T00:00:00Z"
      }
    ]
  }
]

This importer extracts human messages as individual memories.
"""

from __future__ import annotations

import json
import logging

from remembra.io.importers import ImportedMemory

logger = logging.getLogger(__name__)


def parse_claude_export(data: str | bytes) -> list[ImportedMemory]:
    """Parse a Claude conversation export.

    Args:
        data: Raw JSON string or bytes.

    Returns:
        List of ImportedMemory records (one per human message).
    """
    try:
        conversations = json.loads(data)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude export: %s", e)
        return []

    if not isinstance(conversations, list):
        conversations = [conversations]

    memories: list[ImportedMemory] = []

    for conv in conversations:
        title = conv.get("name", "Untitled")
        messages = conv.get("chat_messages", [])

        for msg in messages:
            sender = msg.get("sender", "")
            if sender != "human":
                continue

            text = msg.get("text", "").strip()
            if not text or len(text) < 10:
                continue

            memories.append(
                ImportedMemory(
                    content=text,
                    metadata={
                        "conversation_title": title,
                        "original_role": "human",
                    },
                    source_format="claude",
                    source_id=msg.get("uuid"),
                    timestamp=msg.get("created_at"),
                )
            )

    logger.info("Parsed %d memories from Claude export", len(memories))
    return memories
