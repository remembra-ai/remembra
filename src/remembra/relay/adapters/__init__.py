"""Adapter registry: one module per agent. See :mod:`remembra.relay.adapters.base`."""

from __future__ import annotations

from remembra.relay.adapters import claude_code, codex, cursor, gemini, kimi, qwen
from remembra.relay.adapters.base import Adapter, AdapterSpec, Change, PayloadMap, backup_and_write, relay_command

REGISTRY: dict[str, Adapter] = {
    module.ADAPTER.spec.name: module.ADAPTER for module in (claude_code, codex, cursor, gemini, qwen, kimi)
}


def get_adapter(name: str | None) -> Adapter | None:
    return REGISTRY.get((name or "").strip().lower()) if name else None


__all__ = ["REGISTRY", "Adapter", "AdapterSpec", "Change", "PayloadMap", "backup_and_write", "get_adapter", "relay_command"]
