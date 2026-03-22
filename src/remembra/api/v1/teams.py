"""Team collaboration endpoints – /api/v1/teams.

Enables multi-user collaboration with shared memory spaces.
"""

import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator

from remembra.auth.middleware import CurrentUser
from remembra.cloud.email import EmailProvider, EmailService
from remembra.core.limiter import limiter
from remembra.teams.manager import TeamManager

router = APIRouter(prefix="/teams", tags=["teams"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_team_manager(request: Request) -> TeamManager:
    manager = getattr(request.app.state, "team_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Team collaboration is not enabled.",
        )
    return manager


TeamManagerDep = Annotated[TeamManager, Depends(get_team_manager)]


def get_email_service() -> EmailService | None:
    """Get email service if Resend is configured."""
    if os.getenv("RESEND_API_KEY"):
        try:
            return EmailService.create(provider=EmailProvider.RESEND)
        except Exception:
            return None
    return None


EmailServiceDep = Annotated[EmailService | None, Depends(get_email_service)]


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Team name")
    description: str = Field("", max_length=1024, description="Team description")
    slug: str | None = Field(None, max_length=50, description="URL-friendly slug (auto-generated if not provided)")

    @field_validator("name", "description")
    @classmethod
    def sanitize_html(cls, v: str) -> str:
        import re

        clean = re.sub(r"<[^>]+>", "", v)
        clean = re.sub(r"javascript:", "", clean, flags=re.IGNORECASE)
        clean = re.sub(r"on\w+\s*=", "", clean, flags=re.IGNORECASE)
        return clean.strip()


class CreateTeamResponse(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    owner_id: str
    plan: str
    max_seats: int
    used_seats: int
    created_at: str
    role: str


class TeamDetail(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    owner_id: str
    plan: str
    max_seats: int
    used_seats: int
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    created_at: str
    updated_at: str


class TeamSummary(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    owner_id: str
    plan: str
    max_seats: int
    used_seats: int
    created_at: str
    role: str


class UpdateTeamRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = Field(None, max_length=1024)


class MemberInfo(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None
    role: str
    invited_by: str | None = None
    joined_at: str


class InviteMemberRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address to invite")
    role: str = Field("member", description="Role: admin, member, or viewer")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "member", "viewer"):
            raise ValueError("Role must be admin, member, or viewer")
        return v


class InviteResponse(BaseModel):
    id: str
    team_id: str
    team_name: str
    email: str
    role: str
    invite_url: str
    expires_at: str
    created_at: str


class PendingInvite(BaseModel):
    id: str
    email: str
    role: str
    invited_by: str
    status: str
    expires_at: str
    created_at: str


class AcceptInviteRequest(BaseModel):
    token: str = Field(..., description="Invite token from email link")


class AcceptInviteResponse(BaseModel):
    team_id: str
    team_name: str
    role: str
    joined_at: str


class UpdateRoleRequest(BaseModel):
    role: str = Field(..., description="New role: admin, member, or viewer")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "member", "viewer"):
            raise ValueError("Role must be admin, member, or viewer")
        return v


class LinkSpaceRequest(BaseModel):
    space_id: str = Field(..., description="Space ID to link to the team")


# ---------------------------------------------------------------------------
# Team CRUD
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=CreateTeamResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create team",
    description="Create a new team. You become the owner.",
)
@limiter.limit("10/minute")
async def create_team(
    request: Request,
    body: CreateTeamRequest,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> CreateTeamResponse:
    # Look up owner's billing plan to inherit
    from remembra.cloud.plans import PlanTier, get_plan

    plan = "pro"  # Default fallback
    max_seats = 5

    # Try to get the user's actual billing plan
    meter = getattr(request.app.state, "usage_meter", None)
    if meter:
        try:
            tenant = await meter.get_tenant(user.user_id)
            if tenant and tenant.get("plan"):
                plan = tenant["plan"]
                plan_tier = PlanTier(plan)
                plan_limits = get_plan(plan_tier)
                max_seats = plan_limits.max_users
        except Exception:
            pass  # Use defaults if lookup fails

    team = await manager.create_team(
        name=body.name,
        owner_id=user.user_id,
        description=body.description,
        slug=body.slug,
        plan=plan,
        max_seats=max_seats,
    )
    return CreateTeamResponse(**team)


@router.get(
    "",
    response_model=list[TeamSummary],
    summary="List my teams",
    description="List all teams you're a member of.",
)
@limiter.limit("30/minute")
async def list_teams(
    request: Request,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> list[TeamSummary]:
    teams = await manager.list_user_teams(user.user_id)
    return [TeamSummary(**t) for t in teams]


@router.get(
    "/{team_id}",
    response_model=TeamDetail,
    summary="Get team",
    description="Get team details. Must be a team member.",
)
@limiter.limit("30/minute")
async def get_team(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> TeamDetail:
    # Check membership
    membership = await manager.get_membership(team_id, user.user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Team not found or access denied")

    team = await manager.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    return TeamDetail(**team)


@router.patch(
    "/{team_id}",
    response_model=TeamDetail,
    summary="Update team",
    description="Update team settings. Admin or owner only.",
)
@limiter.limit("20/minute")
async def update_team(
    request: Request,
    team_id: str,
    body: UpdateTeamRequest,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> TeamDetail:
    try:
        team = await manager.update_team(
            team_id=team_id,
            user_id=user.user_id,
            name=body.name,
            description=body.description,
        )
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        return TeamDetail(**team)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete(
    "/{team_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Delete team",
    description="Delete the team. Owner only.",
)
@limiter.limit("5/minute")
async def delete_team(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> None:
    try:
        deleted = await manager.delete_team(team_id, user.user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Team not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get(
    "/{team_id}/members",
    response_model=list[MemberInfo],
    summary="List members",
    description="List all team members.",
)
@limiter.limit("30/minute")
async def list_members(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> list[MemberInfo]:
    # Check membership
    membership = await manager.get_membership(team_id, user.user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Team not found or access denied")

    members = await manager.list_members(team_id)
    return [MemberInfo(**m) for m in members]


@router.patch(
    "/{team_id}/members/{member_id}/role",
    response_model=MemberInfo,
    summary="Update member role",
    description="Change a member's role. Admin or owner only.",
)
@limiter.limit("10/minute")
async def update_member_role(
    request: Request,
    team_id: str,
    member_id: str,
    body: UpdateRoleRequest,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> MemberInfo:
    try:
        membership = await manager.update_member_role(
            team_id=team_id,
            user_id=member_id,
            new_role=body.role,
            updated_by=user.user_id,
        )
        if not membership:
            raise HTTPException(status_code=404, detail="Member not found")

        # Get full member info
        members = await manager.list_members(team_id)
        for m in members:
            if m["user_id"] == member_id:
                return MemberInfo(**m)

        raise HTTPException(status_code=404, detail="Member not found")
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.delete(
    "/{team_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Remove member",
    description="Remove a member from the team. Admin or owner only.",
)
@limiter.limit("10/minute")
async def remove_member(
    request: Request,
    team_id: str,
    member_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> None:
    try:
        removed = await manager.remove_member(
            team_id=team_id,
            user_id=member_id,
            removed_by=user.user_id,
        )
        if not removed:
            raise HTTPException(status_code=404, detail="Member not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post(
    "/{team_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Leave team",
    description="Leave the team. Owners cannot leave.",
)
@limiter.limit("10/minute")
async def leave_team(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> None:
    try:
        left = await manager.leave_team(team_id, user.user_id)
        if not left:
            raise HTTPException(status_code=404, detail="Not a member of this team")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@router.post(
    "/{team_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite member",
    description="Send an invite email. Admin or owner only.",
)
@limiter.limit("20/minute")
async def invite_member(
    request: Request,
    team_id: str,
    body: InviteMemberRequest,
    manager: TeamManagerDep,
    email_service: EmailServiceDep,
    user: CurrentUser,
) -> InviteResponse:
    try:
        invite = await manager.create_invite(
            team_id=team_id,
            email=body.email,
            role=body.role,
            invited_by=user.user_id,
        )

        # Build invite URL
        base_url = os.getenv("REMEMBRA_BASE_URL", "https://app.remembra.dev")
        invite_url = f"{base_url}/invite/{invite['token']}"

        # Send invite email
        if email_service:
            try:
                await email_service.send_team_invite_email(
                    to=invite["email"],
                    team_name=invite["team_name"],
                    inviter_email=user.name or "a team member",
                    role=invite["role"],
                    invite_url=invite_url,
                    expires_at=invite["expires_at"],
                )
            except Exception as e:
                # Log but don't fail - invite still created
                import logging

                logging.getLogger(__name__).warning(f"Failed to send invite email: {e}")

        return InviteResponse(
            id=invite["id"],
            team_id=invite["team_id"],
            team_name=invite["team_name"],
            email=invite["email"],
            role=invite["role"],
            invite_url=invite_url,
            expires_at=invite["expires_at"],
            created_at=invite["created_at"],
        )
    except (PermissionError, ValueError) as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get(
    "/{team_id}/invites",
    response_model=list[PendingInvite],
    summary="List pending invites",
    description="List pending team invites. Admin or owner only.",
)
@limiter.limit("30/minute")
async def list_invites(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> list[PendingInvite]:
    # Check admin permission
    membership = await manager.get_membership(team_id, user.user_id)
    if not membership or membership["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="Admin access required")

    invites = await manager.list_pending_invites(team_id)
    return [PendingInvite(**i) for i in invites]


@router.delete(
    "/{team_id}/invites/{invite_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Revoke invite",
    description="Revoke a pending invite. Admin or owner only.",
)
@limiter.limit("10/minute")
async def revoke_invite(
    request: Request,
    team_id: str,
    invite_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> None:
    try:
        revoked = await manager.revoke_invite(invite_id, user.user_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="Invite not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.post(
    "/invites/accept",
    response_model=AcceptInviteResponse,
    summary="Accept invite",
    description="Accept a team invite using the token from the email.",
)
@limiter.limit("10/minute")
async def accept_invite(
    request: Request,
    body: AcceptInviteRequest,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> AcceptInviteResponse:
    try:
        result = await manager.accept_invite(
            token=body.token,
            user_id=user.user_id,
        )
        return AcceptInviteResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Team Spaces
# ---------------------------------------------------------------------------


@router.post(
    "/{team_id}/spaces",
    status_code=status.HTTP_201_CREATED,
    summary="Link space to team",
    description="Link a memory space to the team. Admin or owner only.",
)
@limiter.limit("20/minute")
async def link_space(
    request: Request,
    team_id: str,
    body: LinkSpaceRequest,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> dict[str, Any]:
    try:
        result = await manager.link_space(
            team_id=team_id,
            space_id=body.space_id,
            linked_by=user.user_id,
        )
        return result
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))


@router.get(
    "/{team_id}/spaces",
    response_model=list[str],
    summary="List team spaces",
    description="List all space IDs linked to the team.",
)
@limiter.limit("30/minute")
async def list_team_spaces(
    request: Request,
    team_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> list[str]:
    # Check membership
    membership = await manager.get_membership(team_id, user.user_id)
    if not membership:
        raise HTTPException(status_code=404, detail="Team not found or access denied")

    return await manager.list_team_spaces(team_id)


@router.delete(
    "/{team_id}/spaces/{space_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    response_model=None,
    summary="Unlink space from team",
    description="Unlink a space from the team. Admin or owner only.",
)
@limiter.limit("10/minute")
async def unlink_space(
    request: Request,
    team_id: str,
    space_id: str,
    manager: TeamManagerDep,
    user: CurrentUser,
) -> None:
    try:
        unlinked = await manager.unlink_space(
            team_id=team_id,
            space_id=space_id,
            unlinked_by=user.user_id,
        )
        if not unlinked:
            raise HTTPException(status_code=404, detail="Space not linked to team")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
