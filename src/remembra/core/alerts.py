"""Operator alerts (REL-8).

The 2026-08-21 quota outage was noticed by users, not by the operator. This
module pushes a one-off alert the first time a provider circuit opens because
of ``quota_exhausted`` (or ``auth``), then stays quiet for a cooldown window so
an outage produces one message, not thousands.

Delivery channels (both optional, both fire-and-forget):

* ``REMEMBRA_ALERT_WEBHOOK_URL`` — JSON ``POST`` (Slack/Discord-compatible
  ``text`` field included).
* ``REMEMBRA_ALERT_EMAIL`` — via the configured email backend (Resend/SMTP).

Alert delivery never raises into request paths.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from remembra.core.circuit_breaker import CircuitBreaker, CircuitState
from remembra.core.metrics import ALERTS_SENT
from remembra.core.time import utcnow

log = structlog.get_logger(__name__)

# Breaker-open kinds that need a human (retrying will not fix them).
ALERTING_KINDS = {"quota_exhausted", "auth"}

EmailSender = Callable[[str, str, str], Awaitable[Any]]


class AlertNotifier:
    def __init__(
        self,
        webhook_url: str | None = None,
        email_to: str | None = None,
        email_sender: EmailSender | None = None,
        cooldown_seconds: float = 3600.0,
        service_name: str = "remembra",
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.email_to = email_to
        self.email_sender = email_sender
        self.cooldown_seconds = cooldown_seconds
        self.service_name = service_name
        self._clock = clock
        self._transport = transport
        self._last_sent: dict[str, float] = {}
        self.sent: list[dict[str, Any]] = []  # recent alerts (bounded) for readiness/debug

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url or (self.email_to and self.email_sender))

    def should_send(self, key: str) -> bool:
        now = self._clock()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.cooldown_seconds:
            return False
        self._last_sent[key] = now
        return True

    async def notify(self, event: str, message: str, details: dict[str, Any] | None = None) -> bool:
        """Send one alert (respecting the per-event cooldown). Returns True if delivered anywhere."""
        if not self.should_send(event):
            ALERTS_SENT.inc(event=event, outcome="suppressed")
            return False
        payload = {
            "service": self.service_name,
            "event": event,
            "message": message,
            "details": details or {},
            "at": utcnow().isoformat() + "Z",
            "text": f"[{self.service_name}] {message}",
        }
        self.sent = [*self.sent[-19:], payload]
        log.critical("operator_alert", alert_event=event, message=message, **(details or {}))
        delivered = False
        if self.webhook_url:
            try:
                async with httpx.AsyncClient(timeout=5.0, transport=self._transport) as client:
                    r = await client.post(self.webhook_url, json=payload)
                    r.raise_for_status()
                delivered = True
                ALERTS_SENT.inc(event=event, outcome="webhook_ok")
            except Exception as e:
                ALERTS_SENT.inc(event=event, outcome="webhook_error")
                log.error("operator_alert_webhook_failed", alert_event=event, error=str(e))
        if self.email_to and self.email_sender:
            try:
                await self.email_sender(self.email_to, f"[{self.service_name}] {event}", message)
                delivered = True
                ALERTS_SENT.inc(event=event, outcome="email_ok")
            except Exception as e:
                ALERTS_SENT.inc(event=event, outcome="email_error")
                log.error("operator_alert_email_failed", alert_event=event, error=str(e))
        if not delivered and not self.enabled:
            ALERTS_SENT.inc(event=event, outcome="log_only")
        return delivered

    def breaker_listener(self, spawn: Callable[[Any, str], Any]) -> Callable[[CircuitBreaker, CircuitState, CircuitState], None]:
        """Build a circuit-breaker state listener that alerts on quota/auth opens.

        ``spawn(coro, name)`` schedules the async delivery (the app passes its
        TaskRegistry) because listeners are called synchronously.
        """

        def _listener(breaker: CircuitBreaker, old: CircuitState, new: CircuitState) -> None:
            if new != CircuitState.OPEN or breaker.opened_by_kind not in ALERTING_KINDS:
                return
            kind = breaker.opened_by_kind
            event = f"{breaker.name}_{kind}"
            message = f"{breaker.name} provider circuit OPEN: {kind}. " + (
                "The provider account is out of quota/credits — top it up; requests are failing fast with 503 until then."
                if kind == "quota_exhausted"
                else "The provider rejected the API key — rotate/fix it; requests are failing fast with 503."
            )
            try:
                spawn(
                    self.notify(event, message, {"breaker": breaker.name, "kind": kind}),
                    f"alert:{event}",
                )
            except RuntimeError:
                # No running loop (sync context) — log only.
                log.critical("operator_alert_unsent", alert_event=event, message=message)

        return _listener


def email_sender_from_settings(settings: Any) -> EmailSender | None:
    """Adapter onto the Resend email backend, if an API key is configured."""
    api_key = getattr(settings, "resend_api_key", None)
    if not isinstance(api_key, str) or not api_key:
        return None
    from html import escape

    from remembra.cloud.email import EmailMessage, ResendBackend

    backend = ResendBackend(api_key=api_key)

    async def _send(to: str, subject: str, body: str) -> Any:
        result = await asyncio.wait_for(
            backend.send(EmailMessage(to=to, subject=subject, html=f"<pre>{escape(body)}</pre>")),
            timeout=10.0,
        )
        if not result.success:
            raise RuntimeError(result.error or "email send failed")
        return result

    return _send
