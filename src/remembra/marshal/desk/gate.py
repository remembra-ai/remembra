"""Who may use the desk: a dashboard login, on an account it is open to, that has not turned it off.

The checks run in this order, the first failure answering (plan 2.1):

1. ``REMEMBRA_MARSHAL_ENABLED`` off -> 404 ``marshal_unavailable``;
2. not a dashboard login (an API key, a connector grant, the desk's own
   in-process principal, auth disabled) -> 403 ``marshal_login_required``;
3. ``REMEMBRA_MARSHAL_ALLOW_USERS`` set and the account not in it -> 404;
4. (board and ask only) the account turned the desk off -> 403 ``marshal_opted_out``.

Authentication (401) runs before all of them, and the app-level write refusal
for delegated principals before that. Errors use the crew routes' body:
``{"detail": {"error": <code>, "message": <sentence>, ...}}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from remembra.auth.middleware import AuthenticatedUser, get_current_user
from remembra.config import get_settings

JWT_PRINCIPAL = "jwt_auth"

UNAVAILABLE = "Marshal isn't available on this account."
LOGIN_REQUIRED = "Marshal answers in a dashboard login only. API keys and connected apps can't use it."
OPTED_OUT = "Marshal desk is off for this account. Turn it on in Settings > Diagnostics."


def desk_error(status_code: int, error: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error, "message": message, **extra})


async def desk_enabled_for(db: Any, user_id: str) -> bool:
    """The account's opt-out (``marshal_prefs.desk``); no row means the desk is on."""
    cursor = await db.conn.execute("SELECT desk FROM marshal_prefs WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    return True if row is None else bool(row[0])


async def set_desk_enabled(db: Any, user_id: str, desk: bool) -> None:
    async with db.transaction():
        await db.conn.execute(
            "INSERT INTO marshal_prefs (user_id, desk, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET desk = excluded.desk, updated_at = excluded.updated_at",
            (user_id, 1 if desk else 0, datetime.now(UTC).isoformat()),
        )


async def desk_user_allow_opted_out(
    request: Request, user: Annotated[AuthenticatedUser, Depends(get_current_user)]
) -> AuthenticatedUser:
    """The gate without the opt-out step (the settings routes, which turn the desk back on)."""
    settings = get_settings()
    if not settings.marshal_enabled:
        raise desk_error(status.HTTP_404_NOT_FOUND, "marshal_unavailable", UNAVAILABLE)
    if user.api_key_id != JWT_PRINCIPAL:
        raise desk_error(status.HTTP_403_FORBIDDEN, "marshal_login_required", LOGIN_REQUIRED)
    allowed = [u.strip() for u in settings.marshal_allow_users if u.strip()]
    explicitly_empty = not allowed and "marshal_allow_users" in settings.model_fields_set
    if explicitly_empty or (allowed and user.user_id not in allowed):
        raise desk_error(status.HTTP_404_NOT_FOUND, "marshal_unavailable", UNAVAILABLE)
    return user


async def desk_user(
    request: Request, user: Annotated[AuthenticatedUser, Depends(desk_user_allow_opted_out)]
) -> AuthenticatedUser:
    """The whole gate (board and ask)."""
    if not await desk_enabled_for(request.app.state.db, user.user_id):
        raise desk_error(status.HTTP_403_FORBIDDEN, "marshal_opted_out", OPTED_OUT)
    return user


DeskUser = Annotated[AuthenticatedUser, Depends(desk_user)]
DeskSettingsUser = Annotated[AuthenticatedUser, Depends(desk_user_allow_opted_out)]
