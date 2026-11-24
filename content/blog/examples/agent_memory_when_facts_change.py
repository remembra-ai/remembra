"""Companion script for the post "When facts change".

    pip install remembra
    export REMEMBRA_API_KEY=...        # from https://app.remembra.dev
    python agent_memory_when_facts_change.py

Stores a fact, replaces it, and asks the same question three ways: now, with
history, and as of the moment before the change. It writes to the project
"facts-change-demo" and deletes what it stored when it finishes.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Any

from remembra import Memory, RecallResult

QUESTION = "What is the Atlas API rate limit?"


def show(label: str, result: RecallResult) -> None:
    print(f"\n{label}")
    for item in result.memories:
        print(f"  {item.relevance:.2f}  {item.content}")


def run(memory: Memory, pause: float = 2.0) -> dict[str, list[str]]:
    """Run the walkthrough and return what each recall answered, by label."""
    seen: dict[str, list[str]] = {}

    def ask(label: str, **options: Any) -> None:
        result = memory.recall(QUESTION, limit=3, **options)
        show(label, result)
        seen[label] = [item.content for item in result.memories]

    first = memory.store("The Atlas API rate limit is 60 requests per minute per API key.")
    memory.store("Atlas sends transactional email through Postmark.")
    print("stored:", first.extracted_facts)
    ask("1. before the change")

    time.sleep(pause)
    before_change = datetime.now(UTC).isoformat()
    time.sleep(pause)

    second = memory.store("The Atlas API rate limit was raised to 120 requests per minute per API key.")
    print("\nstored:", second.extracted_facts)

    ask("2. after the change")
    ask("3. with history", include_superseded=True)
    ask("4. as of before the change", as_of=before_change)
    return seen


def main() -> None:
    memory = Memory(
        base_url="https://api.remembra.dev",
        api_key=os.environ["REMEMBRA_API_KEY"],
        project="facts-change-demo",
    )
    try:
        run(memory)
    finally:
        memory.forget_project("facts-change-demo")
        memory.close()


if __name__ == "__main__":
    main()
