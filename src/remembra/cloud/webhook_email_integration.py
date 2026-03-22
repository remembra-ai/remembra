"""
Example integration of email sending with Stripe webhooks.

This module shows how to send emails based on Stripe webhook events.
Add this logic to your webhook handler in the API.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from remembra.cloud.email import EmailProvider, EmailService

logger = logging.getLogger(__name__)


class StripeWebhookEmailHandler:
    """Handles email notifications for Stripe webhook events.
    
    Example usage in your webhook endpoint:
    
        email_handler = StripeWebhookEmailHandler(email_service)
        await email_handler.handle_event(stripe_event)
    """

    def __init__(self, email_service: EmailService) -> None:
        self.email_service = email_service

    async def handle_event(self, event: dict[str, Any]) -> None:
        """Process a Stripe webhook event and send appropriate emails.
        
        Args:
            event: Stripe event object from webhook
        """
        event_type = event.get("type")
        
        handlers = {
            "invoice.payment_succeeded": self._handle_payment_success,
            "invoice.payment_failed": self._handle_payment_failed,
            "customer.subscription.deleted": self._handle_subscription_deleted,
            "customer.subscription.updated": self._handle_subscription_updated,
        }
        
        handler = handlers.get(event_type)
        if handler:
            try:
                await handler(event["data"]["object"])
                logger.info("Email sent for event: %s", event_type)
            except Exception as e:
                logger.error(
                    "Failed to send email for event %s: %s",
                    event_type,
                    str(e),
                )
        else:
            logger.debug("No email handler for event: %s", event_type)

    async def _handle_payment_success(self, invoice: dict[str, Any]) -> None:
        """Send payment receipt email after successful payment."""
        customer_email = invoice.get("customer_email")
        if not customer_email:
            logger.warning("No customer email in invoice")
            return
        
        amount_paid = invoice.get("amount_paid", 0) / 100  # Convert cents to dollars
        invoice_url = invoice.get("hosted_invoice_url", "#")
        
        # Get billing period dates
        period_start = self._format_timestamp(invoice.get("period_start"))
        period_end = self._format_timestamp(invoice.get("period_end"))
        
        # Determine plan from invoice
        plan = "Pro"  # Default - you might parse this from invoice items
        
        await self.email_service.send_payment_receipt_email(
            to=customer_email,
            amount=f"${amount_paid:.2f}",
            invoice_url=invoice_url,
            plan=plan,
            period_start=period_start,
            period_end=period_end,
        )

    async def _handle_payment_failed(self, invoice: dict[str, Any]) -> None:
        """Send payment failed email when payment fails."""
        customer_email = invoice.get("customer_email")
        if not customer_email:
            # Try to get from customer object
            customer_id = invoice.get("customer")
            customer_email = await self._get_customer_email(customer_id)
        
        if not customer_email:
            logger.warning("No customer email for payment failed notification")
            return
        
        amount_due = invoice.get("amount_due", 0) / 100  # Convert cents to dollars
        
        # Get next retry date (Stripe retries 3-4 times over ~3 weeks)
        next_attempt = invoice.get("next_payment_attempt")
        retry_date = self._format_timestamp(next_attempt) if next_attempt else "soon"
        
        # Determine plan from invoice lines
        plan = "Pro"  # Default
        lines = invoice.get("lines", {}).get("data", [])
        if lines:
            description = lines[0].get("description", "")
            if "team" in description.lower():
                plan = "Team"
        
        await self.email_service.send_payment_failed_email(
            to=customer_email,
            amount=f"${amount_due:.2f}",
            plan=plan,
            retry_date=retry_date,
        )
        logger.info("Payment failed email sent to: %s", customer_email)

    async def _handle_subscription_deleted(self, subscription: dict[str, Any]) -> None:
        """Send cancellation email when subscription is cancelled."""
        # Get customer email from subscription
        customer_id = subscription.get("customer")
        customer_email = await self._get_customer_email(customer_id)
        
        if not customer_email:
            logger.warning("No customer email for subscription deletion")
            return
        
        # Get cancellation date
        cancel_date = self._format_timestamp(subscription.get("current_period_end"))
        
        # Determine plan
        plan = "Pro"  # Parse from subscription items if needed
        
        await self.email_service.send_subscription_cancelled_email(
            to=customer_email,
            plan=plan,
            cancel_date=cancel_date,
        )

    async def _handle_subscription_updated(self, subscription: dict[str, Any]) -> None:
        """Handle subscription updates (optional)."""
        # This could be used for plan upgrades/downgrades
        # For now, we just log it
        logger.info(
            "Subscription updated: %s status=%s",
            subscription.get("id"),
            subscription.get("status"),
        )

    def _format_timestamp(self, timestamp: int | None) -> str:
        """Format Unix timestamp to readable date.
        
        Args:
            timestamp: Unix timestamp in seconds
        
        Returns:
            Formatted date string (e.g., "March 1, 2026")
        """
        if not timestamp:
            return "Unknown"
        
        dt = datetime.fromtimestamp(timestamp)
        return dt.strftime("%B %d, %Y")

    async def _get_customer_email(self, customer_id: str) -> str | None:
        """Get customer email from Stripe customer ID.
        
        Fetches the customer from Stripe API to get their email.
        
        Args:
            customer_id: Stripe customer ID
        
        Returns:
            Customer email address or None
        """
        if not customer_id:
            return None
        
        try:
            import os

            import stripe
            
            stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
            if not stripe.api_key:
                logger.warning("STRIPE_SECRET_KEY not set, cannot fetch customer email")
                return None
            
            customer = stripe.Customer.retrieve(customer_id)
            return customer.get("email")
        except Exception as e:
            logger.error("Failed to fetch customer email from Stripe: %s", str(e))
            return None


# Example usage in webhook endpoint
async def example_webhook_handler(stripe_event: dict[str, Any]) -> None:
    """Example of how to use the email handler in your webhook endpoint."""
    
    # Initialize email service (do this once at app startup)
    email_service = EmailService.create(provider=EmailProvider.RESEND)
    
    # Create email handler
    email_handler = StripeWebhookEmailHandler(email_service)
    
    # Process the event
    await email_handler.handle_event(stripe_event)
