"""The one-time account check after the mailbox is first proven (dashboard session only).

* ``GET  /api/v1/auth/review``           is a check pending, and (for a session that proved the mailbox) its items
* ``POST /api/v1/auth/review/revoke``    revoke one item (key, connection, webhook, identity, two_factor, password)
* ``POST /api/v1/auth/review/complete``  keep everything still listed and finish (one click)
* ``POST /api/v1/auth/review/defer``     "later": recorded; the dashboard asks again next session

See :mod:`remembra.auth.account_review` for who may act and why.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Body, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.api.v1.auth import CurrentUser, get_user_manager
from remembra.auth import account_review
from remembra.auth.middleware import get_client_ip
from remembra.core.limiter import limiter

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_METHOD = "dashboard_session"


class ReviewResponse(BaseModel):
    pending: bool = Field(description="A check of credentials set up before the email was verified is open")
    can_review: bool = Field(False, description="This session proved the mailbox and may act on the check")
    origin: str | None = Field(None, description="google | password_reset")
    verified_at: str | None = None
    message: str | None = Field(None, description="Why this session cannot act on the check")
    items: dict[str, Any] | None = None


class RevokeRequest(BaseModel):
    kind: Literal["key", "connection", "webhook", "identity", "two_factor", "password"]
    id: str | None = Field(None, max_length=256, description="Item id (not needed for two_factor / password)")


class RevokeResponse(BaseModel):
    revoked: str
    access_token: str | None = Field(None, description="A fresh session when the old ones were signed out")
    review: ReviewResponse


class CompleteResponse(BaseModel):
    done: bool = True
    kept: list[str]
    removed: list[str]


async def _load(request: Request, current_user: dict[str, Any]) -> tuple[Any, account_review.Review | None, bool]:
    user_manager = await get_user_manager(request)
    review = await account_review.pending_review(user_manager.db, current_user["id"])
    payload = user_manager.verify_jwt_token(current_user["token"])
    return user_manager, review, account_review.session_is_trusted(review, payload)


async def _response(db: Any, review: account_review.Review | None, trusted: bool) -> ReviewResponse:
    if review is None:
        return ReviewResponse(pending=False)
    if not trusted:
        return ReviewResponse(pending=True, can_review=False, message=account_review.blocked_message(review))
    return ReviewResponse(
        pending=True,
        can_review=True,
        origin=review.origin,
        verified_at=account_review._iso(review.verified_at),
        items=await account_review.review_items(db, review),
    )


def _require(review: account_review.Review | None, trusted: bool) -> account_review.Review:
    if review is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="There is nothing to check.")
    if not trusted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=account_review.blocked_message(review))
    return review


@router.get("/review", response_model=ReviewResponse, summary="Pending account check")
@limiter.limit("60/minute")
async def get_account_review(request: Request, current_user: CurrentUser) -> ReviewResponse:
    user_manager, review, trusted = await _load(request, current_user)
    return await _response(user_manager.db, review, trusted)


@router.post("/review/revoke", response_model=RevokeResponse, summary="Revoke one item in the account check")
@limiter.limit("60/minute")
async def revoke_review_item(
    request: Request, body: Annotated[RevokeRequest, Body(...)], current_user: CurrentUser
) -> RevokeResponse:
    user_manager, review, trusted = await _load(request, current_user)
    review = _require(review, trusted)
    db = user_manager.db
    try:
        result = await account_review.revoke_item(db, review, body.kind, body.id, ip=get_client_ip(request), method=_METHOD)
    except account_review.ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e
    access_token = None
    if result["sessions_reset"]:
        # Every session was signed out (so a squatter's is too); this one continues.
        access_token = user_manager.create_jwt_token(
            current_user["id"], current_user["email"], extra_claims={account_review.REVIEW_CLAIM: review.review_id}
        )
    fresh = await account_review.pending_review(db, current_user["id"])
    return RevokeResponse(revoked=result["label"], access_token=access_token, review=await _response(db, fresh, True))


@router.post("/review/complete", response_model=CompleteResponse, summary="Keep the rest and finish the account check")
@limiter.limit("20/minute")
async def complete_account_review(request: Request, current_user: CurrentUser) -> CompleteResponse:
    user_manager, review, trusted = await _load(request, current_user)
    review = _require(review, trusted)
    try:
        result = await account_review.complete(user_manager.db, review, ip=get_client_ip(request), method=_METHOD)
    except account_review.ReviewError as e:
        raise HTTPException(status_code=e.status, detail=e.message) from e
    return CompleteResponse(kept=result["kept"], removed=result["removed"])


@router.post("/review/defer", summary="Check later (asked again next session)")
@limiter.limit("20/minute")
async def defer_account_review(request: Request, current_user: CurrentUser) -> dict[str, bool]:
    user_manager, review, trusted = await _load(request, current_user)
    review = _require(review, trusted)
    await account_review.defer(user_manager.db, review, ip=get_client_ip(request), method=_METHOD)
    return {"deferred": True}
