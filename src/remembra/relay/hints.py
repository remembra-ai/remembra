"""Which project the server may give a location it has not seen: one rule for every local client.

``remembra-relay`` (its hooks and ``resolve``) and ``remembra-crewd`` (a crew join, and SessionStart's
check for an existing crew) send these fields next to a location, so a repository lands in the same
project, and so in the same crew, whichever of them asks. The rule (the relay guide's "Which project a
repository uses"):

* A git repository always gets its own project. The configured project (``REMEMBRA_PROJECT`` from the
  environment, the MCP env or ``~/.remembra/credentials``; ``default`` does not count) is sent as
  ``hint_project`` with ``hint_scope=folders``, so the server applies it only to a folder that is not a
  repository (and, for a key restricted to projects, to a repository it may not otherwise use).
* ``REMEMBRA_RELAY_PROJECT`` opts into one project for everything, repositories included
  (``hint_scope=all``, what 0.16.0 did with ``REMEMBRA_PROJECT``).
* ``git_repo`` tells the server whether this is a repository (a new one may have no commit or remote
  yet). When git did not answer in time neither ``git_repo`` nor the configured project is sent: whether
  this is a repository is unknown, and the configured project must not name a repository.

A request with ``hint_project`` and no ``hint_scope`` is a 0.16.0 client's to the server (``all``): the
configured project would name every new repository. Never send one without the other.
"""

from __future__ import annotations

from typing import Any

from remembra.client.project import normalize_project_id, parse_project_aliases
from remembra.relay.config import RelayConfig
from remembra.relay.identity import HINT_SCOPE_ALL, HINT_SCOPE_FOLDERS

RELAY_PROJECT_ENV = "REMEMBRA_RELAY_PROJECT"
HINT_KEYS = ("hint_project", "hint_scope", "git_repo")


def normalize_project(value: str | None, config: RelayConfig) -> str | None:
    """``value`` as a project id (the config's aliases applied); None when empty or ``default``."""
    if not value or not value.strip():
        return None
    project = normalize_project_id(value, parse_project_aliases(config.project_aliases))
    return project if project and project != "default" else None


def hint_fields(config: RelayConfig, git_repo: bool | None, relay_project: str | None) -> dict[str, Any]:
    """The fields to send with a location (see the module docstring).

    ``git_repo`` is :attr:`remembra.relay.facts.RepoInfo.git_repo` (None: git did not answer in time);
    ``relay_project`` is ``REMEMBRA_RELAY_PROJECT`` as the caller's environment has it.
    """
    fields: dict[str, Any] = {}
    if git_repo is not None:
        fields["git_repo"] = git_repo
    single = normalize_project(relay_project, config)
    if single:
        fields.update(hint_project=single, hint_scope=HINT_SCOPE_ALL)
        return fields
    fields["hint_scope"] = HINT_SCOPE_FOLDERS
    configured = normalize_project(config.project, config)
    if configured and git_repo is not None:
        fields["hint_project"] = configured
    return fields
