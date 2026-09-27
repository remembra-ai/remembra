"""
Email delivery for Remembra Cloud.

Sends the transactional emails of Remembra Relay (welcome, email
verification, password reset, API key created, plan changed, payment failed,
subscription ended, memory cap warnings, team invites, sign-in method added).
Their content lives in :mod:`remembra.cloud.email_templates`: every email has
an HTML and a plain-text part, prices come from the plan catalog, and no email
ever carries an API key.

Supports Resend (production) and SMTP with one interface. Mail is sent from
``REMEMBRA_EMAIL_FROM`` with ``Reply-To: REMEMBRA_EMAIL_REPLY_TO`` (support), so
a reply reaches a person.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast

from remembra.cloud import email_templates as templates
from remembra.cloud.email_templates import RenderedEmail
from remembra.cloud.plans import BillingInterval, PlanTier

logger = logging.getLogger(__name__)

DEFAULT_FROM = "Remembra <noreply@remembra.dev>"
DEFAULT_REPLY_TO = templates.SUPPORT_EMAIL


class EmailProvider(Enum):
    """Available email providers."""

    RESEND = "resend"
    SMTP = "smtp"


def _sender_defaults() -> tuple[str, str | None]:
    try:
        from remembra.config import get_settings

        settings = get_settings()
        return settings.email_from or DEFAULT_FROM, settings.email_reply_to or None
    except Exception:  # settings unavailable (scripts): the production defaults
        return DEFAULT_FROM, DEFAULT_REPLY_TO


@dataclass
class EmailMessage:
    """Email message data. ``from_email`` / ``reply_to`` default to the configured sender."""

    to: str
    subject: str
    html: str
    text: str | None = None
    from_email: str | None = None
    reply_to: str | None = None
    tags: dict[str, str] | None = None

    def __post_init__(self) -> None:
        default_from, default_reply_to = _sender_defaults()
        if not self.from_email:
            self.from_email = default_from
        if self.reply_to is None:
            self.reply_to = default_reply_to


@dataclass
class EmailResult:
    """Result of an email send operation."""

    success: bool
    message_id: str | None = None
    error: str | None = None


class EmailBackend(ABC):
    """Abstract email backend."""

    @abstractmethod
    async def send(self, message: EmailMessage) -> EmailResult:
        """Send an email message."""
        pass


class ResendBackend(EmailBackend):
    """Resend email backend.

    Uses ``REMEMBRA_RESEND_API_KEY`` (settings; ``RESEND_API_KEY`` also works).
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or _configured_resend_key()
        if not self.api_key:
            raise ValueError("REMEMBRA_RESEND_API_KEY (or RESEND_API_KEY) is required for the Resend backend")

    async def send(self, message: EmailMessage) -> EmailResult:
        """Send email via Resend API."""
        try:
            # Import here to avoid requiring resend package if not used
            import resend

            resend.api_key = self.api_key

            params: dict[str, Any] = {
                "from": message.from_email,
                "to": message.to,
                "subject": message.subject,
                "html": message.html,
            }
            if message.text:
                params["text"] = message.text

            if message.reply_to:
                params["reply_to"] = message.reply_to

            if message.tags:
                params["tags"] = [{"name": k, "value": v} for k, v in message.tags.items()]

            # The Resend SDK is synchronous: keep it off the event loop.
            response = await asyncio.to_thread(resend.Emails.send, cast("resend.Emails.SendParams", params))

            logger.info(
                "Email sent via Resend: subject=%s id=%s",
                message.subject,
                response.get("id"),
            )

            return EmailResult(
                success=True,
                message_id=response.get("id"),
            )

        except Exception as e:
            logger.error(
                "Failed to send email via Resend: error=%s",
                str(e),
            )
            return EmailResult(
                success=False,
                error=str(e),
            )


class SMTPBackend(EmailBackend):
    """SMTP email backend.

    Supports Gmail, Google Workspace, and other SMTP servers.
    Requires environment variables:
    - SMTP_HOST
    - SMTP_PORT
    - SMTP_USERNAME
    - SMTP_PASSWORD
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
    ) -> None:
        self.host = host or os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.port = port or int(os.getenv("SMTP_PORT", "587"))
        self.username = username or os.getenv("SMTP_USERNAME")
        self.password = password or os.getenv("SMTP_PASSWORD")
        self.use_tls = use_tls

        if not self.username or not self.password:
            raise ValueError("SMTP_USERNAME and SMTP_PASSWORD environment variables required")

    async def send(self, message: EmailMessage) -> EmailResult:
        """Send email via SMTP."""
        try:
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            import aiosmtplib

            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = message.subject
            msg["From"] = message.from_email or DEFAULT_FROM
            msg["To"] = message.to

            if message.reply_to:
                msg["Reply-To"] = message.reply_to

            # Plain text first, HTML last: clients show the last part they support.
            if message.text:
                msg.attach(MIMEText(message.text, "plain", "utf-8"))
            msg.attach(MIMEText(message.html, "html", "utf-8"))

            # Send email
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                use_tls=self.use_tls,
            )

            logger.info(
                "Email sent via SMTP: subject=%s",
                message.subject,
            )

            return EmailResult(success=True)

        except Exception as e:
            logger.error(
                "Failed to send email via SMTP: error=%s",
                str(e),
            )
            return EmailResult(
                success=False,
                error=str(e),
            )


def _configured_resend_key() -> str | None:
    try:
        from remembra.config import get_settings

        key = get_settings().resend_api_key
    except Exception:
        key = None
    return key or os.getenv("RESEND_API_KEY") or None


def email_service_or_none() -> EmailService | None:
    """The Resend-backed service when a key is configured, else None (email off)."""
    try:
        return EmailService.create(provider=EmailProvider.RESEND)
    except ValueError:  # no Resend key: email is off
        return None
    except Exception as e:  # bad config: email stays off, never fails a request
        logger.warning("email_service_unavailable: %s", type(e).__name__)
        return None


def _dashboard_base() -> str:
    try:
        from remembra.config import get_settings

        return get_settings().public_dashboard_url or templates.DEFAULT_DASHBOARD_URL
    except Exception:
        return templates.DEFAULT_DASHBOARD_URL


def _display_time(value: str | datetime) -> str:
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


class EmailService:
    """Renders a template and sends it through the configured backend.

    Example:
        ```python
        service = email_service_or_none()
        if service:
            await service.send_welcome_email(to="user@example.com", verify_url=url)
        ```
    """

    def __init__(self, backend: EmailBackend, dashboard_url: str | None = None) -> None:
        self.backend = backend
        self._dashboard_url = dashboard_url

    @property
    def dashboard(self) -> str:
        return self._dashboard_url or _dashboard_base()

    @classmethod
    def create(
        cls,
        provider: EmailProvider = EmailProvider.RESEND,
        **kwargs: Any,
    ) -> EmailService:
        """Create an EmailService with the specified provider."""
        backend: EmailBackend
        if provider == EmailProvider.RESEND:
            backend = ResendBackend(**kwargs)
        elif provider == EmailProvider.SMTP:
            backend = SMTPBackend(**kwargs)
        else:
            raise ValueError(f"Unknown email provider: {provider}")

        return cls(backend=backend)

    async def send_rendered(self, to: str, email: RenderedEmail) -> EmailResult:
        """Send a rendered email (HTML + text). Never raises: failures come back as a result."""
        try:
            result = await self.backend.send(
                EmailMessage(to=to, subject=email.subject, html=email.html, text=email.text, tags={"template": email.template})
            )
        except Exception as e:
            logger.error("Email send error: template=%s error=%s", email.template, type(e).__name__)
            return EmailResult(success=False, error=str(e))
        if result.success:
            logger.info("Email sent: template=%s", email.template)
        else:
            logger.warning("Email send failed: template=%s error=%s", email.template, result.error)
        return result

    async def send_welcome_email(self, to: str, *, verify_url: str | None = None, dashboard_account: bool = True) -> EmailResult:
        """Welcome after signup: the install lines and the dashboard, never an API key."""
        return await self.send_rendered(
            to, templates.welcome(dashboard=self.dashboard, verify_url=verify_url, dashboard_account=dashboard_account)
        )

    async def send_email_verification_email(self, to: str, verify_url: str) -> EmailResult:
        return await self.send_rendered(to, templates.email_verification(dashboard=self.dashboard, verify_url=verify_url))

    async def send_password_reset_email(
        self, to: str, reset_url: str, *, expires_hours: int, email_verified: bool = True
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.password_reset(
                dashboard=self.dashboard, reset_url=reset_url, expires_hours=expires_hours, email_verified=email_verified
            ),
        )

    async def send_key_created_email(
        self,
        to: str,
        *,
        key_name: str | None,
        role: str,
        project_ids: list[str] | None = None,
        agent_id: str | None = None,
        created_at: datetime,
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.key_created(
                dashboard=self.dashboard,
                key_name=key_name,
                role=role,
                project_ids=project_ids or [],
                agent_id=agent_id,
                created_at=created_at,
            ),
        )

    async def send_plan_changed_email(
        self,
        to: str,
        *,
        old_tier: PlanTier | None,
        new_tier: PlanTier,
        interval: BillingInterval | None,
        seats: int | None,
        founding: bool,
        memory_cap: int,
        founding_held: bool = True,
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.plan_changed(
                dashboard=self.dashboard,
                old_tier=old_tier,
                new_tier=new_tier,
                interval=interval,
                seats=seats,
                founding=founding,
                founding_held=founding_held,
                memory_cap=memory_cap,
            ),
        )

    async def send_payment_failed_email(
        self,
        to: str,
        *,
        tier: PlanTier,
        interval: BillingInterval | None,
        seats: int | None,
        founding: bool | None,
        founding_held: bool = True,
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.payment_failed(
                dashboard=self.dashboard,
                tier=tier,
                interval=interval,
                seats=seats,
                founding=founding,
                founding_held=founding_held,
            ),
        )

    async def send_subscription_cancelled_email(self, to: str, *, old_tier: PlanTier, memory_cap: int) -> EmailResult:
        return await self.send_rendered(
            to, templates.subscription_cancelled(dashboard=self.dashboard, old_tier=old_tier, memory_cap=memory_cap)
        )

    async def send_usage_warning_email(
        self, to: str, *, usage_percent: int, current_usage: int, limit: int, plan: PlanTier | str
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.usage_warning(
                dashboard=self.dashboard,
                tier=PlanTier(plan),
                usage_percent=usage_percent,
                current_usage=current_usage,
                limit=limit,
            ),
        )

    async def send_limit_exceeded_email(self, to: str, *, current_usage: int, limit: int, plan: PlanTier | str) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.limit_exceeded(dashboard=self.dashboard, tier=PlanTier(plan), current_usage=current_usage, limit=limit),
        )

    async def send_account_deletion_code_email(self, to: str, code: str) -> EmailResult:
        """Send the six-digit code that confirms an account deletion (inline HTML; no template file)."""
        import html as _html

        safe_code = _html.escape(code)
        message = EmailMessage(
            to=to,
            subject="Remembra: your account deletion code",
            html=(
                "<p>Someone signed in to your Remembra account asked to delete it.</p>"
                f'<p style="font-size:24px;font-weight:700;letter-spacing:4px">{safe_code}</p>'
                "<p>Enter this code in Settings to confirm. It expires in 15 minutes. Deleting cancels any "
                "subscription immediately and erases all your data after the grace period.</p>"
                "<p>If this was not you, do not share the code: sign in and change your password.</p>"
            ),
            tags={"template": "account_deletion_code"},
        )
        return await self.backend.send(message)

    async def send_team_invite_email(
        self,
        to: str,
        team_name: str,
        inviter_email: str,
        role: str,
        invite_url: str,
        expires_at: str | datetime,
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.team_invite(
                dashboard=self.dashboard,
                team_name=team_name,
                inviter_email=inviter_email,
                role=role,
                invite_url=invite_url,
                expires_at=_display_time(expires_at),
            ),
        )

    async def send_account_deleted_email(
        self, to: str, *, deleted_on: str, erase_after: str, subscriptions_cancelled: int
    ) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.account_deleted(
                dashboard=self.dashboard,
                deleted_on=deleted_on,
                erase_after=erase_after,
                subscriptions_cancelled=subscriptions_cancelled,
            ),
        )

    async def send_identity_linked_email(self, to: str, *, provider_name: str, provider_email: str) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.identity_linked(dashboard=self.dashboard, provider_name=provider_name, provider_email=provider_email),
        )

    async def send_account_review_email(self, to: str, *, kept: list[str], removed: list[str]) -> EmailResult:
        return await self.send_rendered(
            to,
            templates.account_review_done(dashboard=self.dashboard, kept=kept, removed=removed),
        )
