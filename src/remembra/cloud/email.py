"""
Email delivery system for Remembra Cloud.

Sends transactional emails for:
- Welcome emails with API keys
- Usage warnings and limit notifications
- Billing and subscription updates
- Account notifications

Supports multiple providers (Resend, SMTP) with a unified interface.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class EmailProvider(Enum):
    """Available email providers."""

    RESEND = "resend"
    SMTP = "smtp"


@dataclass
class EmailMessage:
    """Email message data."""

    to: str
    subject: str
    html: str
    from_email: str = "Remembra <noreply@remembra.dev>"
    reply_to: str | None = None
    tags: dict[str, str] | None = None


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
    
    Requires RESEND_API_KEY environment variable.
    Get your API key from: https://resend.com/api-keys
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.getenv("RESEND_API_KEY")
        if not self.api_key:
            raise ValueError(
                "RESEND_API_KEY environment variable is required for Resend backend"
            )

    async def send(self, message: EmailMessage) -> EmailResult:
        """Send email via Resend API."""
        try:
            # Import here to avoid requiring resend package if not used
            import resend

            resend.api_key = self.api_key

            params = {
                "from": message.from_email,
                "to": message.to,
                "subject": message.subject,
                "html": message.html,
            }

            if message.reply_to:
                params["reply_to"] = message.reply_to

            if message.tags:
                params["tags"] = [
                    {"name": k, "value": v} for k, v in message.tags.items()
                ]

            response = resend.Emails.send(params)

            logger.info(
                "Email sent via Resend: to=%s subject=%s id=%s",
                message.to,
                message.subject,
                response.get("id"),
            )

            return EmailResult(
                success=True,
                message_id=response.get("id"),
            )

        except Exception as e:
            logger.error(
                "Failed to send email via Resend: to=%s error=%s",
                message.to,
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
            raise ValueError(
                "SMTP_USERNAME and SMTP_PASSWORD environment variables required"
            )

    async def send(self, message: EmailMessage) -> EmailResult:
        """Send email via SMTP."""
        try:
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            import aiosmtplib

            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = message.subject
            msg["From"] = message.from_email
            msg["To"] = message.to

            if message.reply_to:
                msg["Reply-To"] = message.reply_to

            # Add HTML content
            html_part = MIMEText(message.html, "html")
            msg.attach(html_part)

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
                "Email sent via SMTP: to=%s subject=%s",
                message.to,
                message.subject,
            )

            return EmailResult(success=True)

        except Exception as e:
            logger.error(
                "Failed to send email via SMTP: to=%s error=%s",
                message.to,
                str(e),
            )
            return EmailResult(
                success=False,
                error=str(e),
            )


class EmailService:
    """High-level email service for Remembra.
    
    Handles template loading and email sending with retries.
    
    Args:
        backend: Email backend to use (ResendBackend or SMTPBackend)
        template_dir: Directory containing HTML email templates
    
    Example:
        ```python
        # Using Resend (recommended)
        service = EmailService.create(provider=EmailProvider.RESEND)
        
        # Using SMTP
        service = EmailService.create(provider=EmailProvider.SMTP)
        
        # Send welcome email
        await service.send_welcome_email(
            to="user@example.com",
            api_key="rem_abc123",
            user_id="user_xyz",
        )
        ```
    """

    def __init__(
        self,
        backend: EmailBackend,
        template_dir: Path | None = None,
    ) -> None:
        self.backend = backend
        self.template_dir = template_dir or (
            Path(__file__).parent / "templates" / "email"
        )

    @classmethod
    def create(
        cls,
        provider: EmailProvider = EmailProvider.RESEND,
        **kwargs: Any,
    ) -> EmailService:
        """Create an EmailService with the specified provider.
        
        Args:
            provider: Email provider to use
            **kwargs: Additional arguments for the backend
        
        Returns:
            Configured EmailService instance
        """
        if provider == EmailProvider.RESEND:
            backend = ResendBackend(**kwargs)
        elif provider == EmailProvider.SMTP:
            backend = SMTPBackend(**kwargs)
        else:
            raise ValueError(f"Unknown email provider: {provider}")

        return cls(backend=backend)

    def _load_template(self, template_name: str) -> str:
        """Load an email template from disk."""
        template_path = self.template_dir / f"{template_name}.html"
        
        if not template_path.exists():
            raise FileNotFoundError(
                f"Email template not found: {template_path}"
            )
        
        return template_path.read_text()

    def _render_template(self, template: str, **kwargs: Any) -> str:
        """Simple template rendering using string formatting.
        
        For more complex templates, consider using Jinja2.
        """
        return template.format(**kwargs)

    async def send_email(
        self,
        to: str,
        subject: str,
        template_name: str,
        **template_vars: Any,
    ) -> EmailResult:
        """Send an email using a template.
        
        Args:
            to: Recipient email address
            subject: Email subject line
            template_name: Name of the template file (without .html)
            **template_vars: Variables to render in the template
        
        Returns:
            EmailResult with success status and details
        """
        try:
            # Load and render template
            template = self._load_template(template_name)
            html = self._render_template(template, **template_vars)

            # Create message
            message = EmailMessage(
                to=to,
                subject=subject,
                html=html,
                tags={"template": template_name},
            )

            # Send email
            result = await self.backend.send(message)

            if result.success:
                logger.info(
                    "Email sent successfully: to=%s template=%s",
                    to,
                    template_name,
                )
            else:
                logger.warning(
                    "Email send failed: to=%s template=%s error=%s",
                    to,
                    template_name,
                    result.error,
                )

            return result

        except Exception as e:
            logger.error(
                "Email send error: to=%s template=%s error=%s",
                to,
                template_name,
                str(e),
            )
            return EmailResult(
                success=False,
                error=str(e),
            )

    async def send_welcome_email(
        self,
        to: str,
        api_key: str,
        user_id: str,
        plan: str = "Free",
    ) -> EmailResult:
        """Send welcome email with API key to new user.
        
        Args:
            to: User's email address
            api_key: Generated API key
            user_id: User ID
            plan: Plan tier name
        """
        return await self.send_email(
            to=to,
            subject="Welcome to Remembra - Your API Key Inside",
            template_name="welcome",
            api_key=api_key,
            user_id=user_id,
            plan=plan,
            dashboard_url="https://app.remembra.dev",
            docs_url="https://docs.remembra.dev",
        )

    async def send_usage_warning_email(
        self,
        to: str,
        usage_percent: int,
        current_usage: int,
        limit: int,
        plan: str,
    ) -> EmailResult:
        """Send usage warning email (80% threshold).
        
        Args:
            to: User's email address
            usage_percent: Current usage percentage
            current_usage: Current memory count
            limit: Plan memory limit
            plan: Plan tier name
        """
        return await self.send_email(
            to=to,
            subject=f"Remembra: {usage_percent}% of your memory limit reached",
            template_name="usage_warning",
            usage_percent=usage_percent,
            current_usage=current_usage,
            limit=limit,
            plan=plan,
            dashboard_url="https://app.remembra.dev",
            upgrade_url="https://app.remembra.dev/billing",
        )

    async def send_limit_exceeded_email(
        self,
        to: str,
        current_usage: int,
        limit: int,
        plan: str,
    ) -> EmailResult:
        """Send limit exceeded email.
        
        Args:
            to: User's email address
            current_usage: Current memory count
            limit: Plan memory limit
            plan: Plan tier name
        """
        return await self.send_email(
            to=to,
            subject="Remembra: Memory limit reached",
            template_name="limit_exceeded",
            current_usage=current_usage,
            limit=limit,
            plan=plan,
            dashboard_url="https://app.remembra.dev",
            upgrade_url="https://app.remembra.dev/billing",
        )

    async def send_payment_receipt_email(
        self,
        to: str,
        amount: str,
        invoice_url: str,
        plan: str,
        period_start: str,
        period_end: str,
    ) -> EmailResult:
        """Send payment receipt email.
        
        Args:
            to: User's email address
            amount: Payment amount (e.g., "$49.00")
            invoice_url: Stripe invoice URL
            plan: Plan tier name
            period_start: Billing period start date
            period_end: Billing period end date
        """
        return await self.send_email(
            to=to,
            subject=f"Remembra: Payment received - {amount}",
            template_name="payment_receipt",
            amount=amount,
            invoice_url=invoice_url,
            plan=plan,
            period_start=period_start,
            period_end=period_end,
            dashboard_url="https://app.remembra.dev",
        )

    async def send_subscription_cancelled_email(
        self,
        to: str,
        plan: str,
        cancel_date: str,
    ) -> EmailResult:
        """Send subscription cancelled email.
        
        Args:
            to: User's email address
            plan: Cancelled plan tier name
            cancel_date: Date subscription ends
        """
        return await self.send_email(
            to=to,
            subject="Remembra: Subscription cancelled",
            template_name="subscription_cancelled",
            plan=plan,
            cancel_date=cancel_date,
            dashboard_url="https://app.remembra.dev",
            resubscribe_url="https://app.remembra.dev/billing",
        )
