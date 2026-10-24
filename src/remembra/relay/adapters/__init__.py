"""Adapter registry: one module per agent. See :mod:`remembra.relay.adapters.base`."""

from __future__ import annotations

from pathlib import Path

from remembra.relay.adapters import claude_code, codex, cursor, gemini, kimi, qwen
from remembra.relay.adapters.base import (
    Adapter,
    AdapterSpec,
    Change,
    CloseEvent,
    PayloadMap,
    backup_and_write,
    relay_command,
)

REGISTRY: dict[str, Adapter] = {
    module.ADAPTER.spec.name: module.ADAPTER for module in (claude_code, codex, cursor, gemini, qwen, kimi)
}


def get_adapter(name: str | None) -> Adapter | None:
    return REGISTRY.get((name or "").strip().lower()) if name else None


__all__ = [
    "REGISTRY",
    "Adapter",
    "AdapterSpec",
    "Change",
    "CloseEvent",
    "PayloadMap",
    "backup_and_write",
    "crew_hooks_installed",
    "get_adapter",
    "relay_command",
]


def crew_hooks_installed(home: Path, agent: str, repo: str | None) -> bool | None:
    """True when ``agent``'s crew hooks are written where it reads them (globally, or in ``repo``'s
    project config) and the gate they run exists; False when not; None for an agent without crew hooks.

    A missing gate makes every gate hook allow and do nothing, so hooks without it count as not installed.
    """
    from remembra.crew.schemas import CREW_HOME_REL
    from remembra.relay.adapters.crew_hooks import CREW_MARKER, crew_specs

    adapter = get_adapter(agent)
    spec = crew_specs().get(agent)
    if adapter is None or spec is None:
        return None
    if not (home / CREW_HOME_REL / "bin" / "crew-gate.py").is_file():
        return False
    files = [adapter.spec.config_file(home)]
    if repo and spec.project_config is not None:
        project = spec.project_config(Path(repo))
        files += [project, project.with_name("settings.local.json")] if project.suffix == ".json" else [project]
    for path in files:
        try:
            if CREW_MARKER in path.read_text(encoding="utf-8"):
                return True
        except (OSError, UnicodeDecodeError):
            continue
    return False
