"""The principal the desk's tools run under: the same user, read-only, one conversation.

It is never a credential: only this process can set it (``connector_principal``)
and only around one in-process GET. ``marshal:`` is a read-only delegated
prefix, so every write route refuses it (``refuse_delegated_writes``), the
account, billing, key and admin routers refuse it outright
(``refuse_delegated_principal``), and RBAC gives it VIEWER. The conversation
id from the dashboard is only hashed, never stored.
"""

from __future__ import annotations

import hashlib

from remembra.auth.middleware import AuthenticatedUser


def conv_hash(user_id: str, conv: str) -> str:
    """The first 12 hex characters of ``sha256("<user_id>:<conv>")``."""
    return hashlib.sha256(f"{user_id}:{conv}".encode()).hexdigest()[:12]


def read_principal(user: AuthenticatedUser, conv: str) -> AuthenticatedUser:
    """The verified dashboard user, reduced to ``memory:recall`` on every one of their projects."""
    return AuthenticatedUser(
        user_id=user.user_id,
        api_key_id=f"marshal:{conv_hash(user.user_id, conv)}",
        rate_limit_tier=user.rate_limit_tier,
        name="marshal",
        role="viewer",
        scopes=["memory:recall"],
        project_ids=None,
        agent_id=None,
    )
