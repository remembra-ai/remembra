"""Platform superadmin checks.

Superadmin is a platform-operator privilege (all tenants), distinct from the
per-tenant ``admin`` key role. A caller is superadmin only when:

* the account is active, and
* its user id is listed in ``REMEMBRA_SUPERADMIN_USER_IDS``, or its email is in
  ``REMEMBRA_OWNER_EMAILS`` **and verified** (an unverified signup that merely
  claims an owner address gets nothing), and
* the request is a dashboard session (JWT), or an API key whose role is
  ``admin`` — ordinary editor/viewer keys never inherit superadmin.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Request, status

from remembra.auth.middleware import AuthenticatedUser, CurrentUser
from remembra.config import get_settings

log = structlog.get_logger(__name__)


def account_is_owner(user_row: dict[str, Any] | None) -> bool:
    """True when a user record qualifies as a platform owner (independent of credential type)."""
    if not user_row or not user_row.get("is_active", True):
        return False
    settings = get_settings()
    if user_row.get("id") and user_row["id"] in (settings.superadmin_user_ids or []):
        return True
    email = (user_row.get("email") or "").strip().lower()
    owner_emails = {e.strip().lower() for e in (settings.owner_emails or []) if e}
    return bool(email) and email in owner_emails and bool(user_row.get("email_verified"))


async def account_is_owner_now(db: Any, user_row: dict[str, Any] | None) -> bool:
    """:func:`account_is_owner`, except that an owner ADDRESS counts only once its account review is done.

    Accounts listed by id in ``REMEMBRA_SUPERADMIN_USER_IDS`` are unaffected.
    An owner address verified by Sign in with Google (or a reset) on an account
    someone else may have pre-registered gives no platform rights until the
    mailbox owner has reviewed the credentials set up before
    (:mod:`remembra.auth.account_review`); otherwise a squatter's admin key
    would inherit superadmin the moment the real owner signs in.
    """
    if not account_is_owner(user_row):
        return False
    assert user_row is not None
    if user_row.get("id") and user_row["id"] in (get_settings().superadmin_user_ids or []):
        return True
    if db is None:
        return True
    from remembra.auth import account_review

    return not await account_review.is_pending(db, str(user_row["id"]))


def credential_may_act_as_superadmin(user: AuthenticatedUser) -> bool:
    """JWT sessions may; API keys only when their role is admin."""
    if user.api_key_id == "jwt_auth":
        return True
    return user.role == "admin"


async def is_superadmin(request: Request, user: AuthenticatedUser) -> bool:
    if not credential_may_act_as_superadmin(user):
        return False
    db = getattr(request.app.state, "db", None)
    if db is None:
        return False
    return await account_is_owner_now(db, await db.get_user_by_id(user.user_id))


async def require_superadmin(request: Request, current_user: CurrentUser) -> None:
    """Dependency: 403 unless the caller is a platform superadmin."""
    if not await is_superadmin(request, current_user):
        log.warning("superadmin_access_denied", user_id=current_user.user_id)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin access required.",
        )


RequireSuperadmin = Annotated[None, Depends(require_superadmin)]
