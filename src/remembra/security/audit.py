"""Audit logging for security monitoring and compliance."""

import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import structlog

from remembra.storage.database import Database

log = structlog.get_logger(__name__)


class AuditAction(StrEnum):
    """Types of auditable actions."""
    
    # Memory operations
    MEMORY_STORE = "memory_store"
    MEMORY_RECALL = "memory_recall"
    MEMORY_FORGET = "memory_forget"
    MEMORY_GET = "memory_get"
    
    # API key operations
    KEY_CREATED = "key_created"
    KEY_UPDATED = "key_updated"
    KEY_REVOKED = "key_revoked"
    KEY_LISTED = "key_listed"
    
    # Authentication events
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILED = "auth_failed"
    AUTH_RATE_LIMITED = "auth_rate_limited"


@dataclass
class AuditEvent:
    """Represents an audit log entry."""
    
    id: str
    timestamp: datetime
    user_id: str
    action: AuditAction
    api_key_id: str | None = None
    resource_id: str | None = None
    ip_address: str | None = None
    success: bool = True
    error_message: str | None = None


class AuditLogger:
    """
    Security audit logger for Remembra.
    
    Logs all memory operations and authentication events for:
    - Security monitoring
    - Incident investigation
    - Compliance (SOC 2, GDPR, etc.)
    
    IMPORTANT: Never logs actual memory content or full API keys.
    Only logs: action type, user_id, key_id (masked), timestamps, success/failure.
    """
    
    def __init__(self, db: Database) -> None:
        self.db = db
    
    @staticmethod
    def generate_audit_id() -> str:
        """Generate unique audit event ID."""
        return f"audit_{secrets.token_urlsafe(16)}"
    
    async def log(
        self,
        user_id: str,
        action: AuditAction,
        api_key_id: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        error_message: str | None = None,
    ) -> AuditEvent:
        """
        Log an audit event.
        
        Args:
            user_id: The user performing the action
            action: Type of action (from AuditAction enum)
            api_key_id: ID of the API key used (if any)
            resource_id: ID of affected resource (memory_id, key_id, etc.)
            ip_address: Client IP address
            success: Whether the action succeeded
            error_message: Error message if action failed
        
        Returns:
            The created AuditEvent
        """
        audit_id = self.generate_audit_id()
        timestamp = datetime.utcnow()
        
        # Store in database
        await self.db.log_audit_event(
            audit_id=audit_id,
            user_id=user_id,
            action=action.value,
            api_key_id=api_key_id,
            resource_id=resource_id,
            ip_address=ip_address,
            success=success,
            error_message=error_message,
        )
        
        # Also log to structured logger for real-time monitoring
        log_func = log.info if success else log.warning
        log_func(
            "audit_event",
            audit_id=audit_id,
            user_id=user_id,
            action=action.value,
            resource_id=resource_id,
            success=success,
        )
        
        return AuditEvent(
            id=audit_id,
            timestamp=timestamp,
            user_id=user_id,
            action=action,
            api_key_id=api_key_id,
            resource_id=resource_id,
            ip_address=ip_address,
            success=success,
            error_message=error_message,
        )
    
    async def log_memory_store(
        self,
        user_id: str,
        memory_id: str,
        api_key_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> AuditEvent:
        """Log a memory store operation."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.MEMORY_STORE,
            api_key_id=api_key_id,
            resource_id=memory_id,
            ip_address=ip_address,
            success=success,
            error_message=error,
        )
    
    async def log_memory_recall(
        self,
        user_id: str,
        api_key_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> AuditEvent:
        """Log a memory recall operation."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.MEMORY_RECALL,
            api_key_id=api_key_id,
            ip_address=ip_address,
            success=success,
            error_message=error,
        )
    
    async def log_memory_forget(
        self,
        user_id: str,
        resource_id: str | None = None,
        api_key_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> AuditEvent:
        """Log a memory forget (delete) operation."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.MEMORY_FORGET,
            api_key_id=api_key_id,
            resource_id=resource_id,
            ip_address=ip_address,
            success=success,
            error_message=error,
        )
    
    async def log_key_created(
        self,
        user_id: str,
        key_id: str,
        ip_address: str | None = None,
    ) -> AuditEvent:
        """Log API key creation."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.KEY_CREATED,
            resource_id=key_id,
            ip_address=ip_address,
            success=True,
        )
    
    async def log_key_updated(
        self,
        user_id: str,
        key_id: str,
        ip_address: str | None = None,
        details: dict | None = None,
    ) -> AuditEvent:
        """Log API key update."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.KEY_UPDATED,
            resource_id=key_id,
            ip_address=ip_address,
            success=True,
            error_message=str(details) if details else None,
        )
    
    async def log_key_revoked(
        self,
        user_id: str,
        key_id: str,
        ip_address: str | None = None,
        success: bool = True,
    ) -> AuditEvent:
        """Log API key revocation."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.KEY_REVOKED,
            resource_id=key_id,
            ip_address=ip_address,
            success=success,
        )
    
    async def log_event(
        self,
        user_id: str,
        action: str,
        resource_id: str | None = None,
        ip_address: str | None = None,
        success: bool = True,
        details: dict | None = None,
    ) -> AuditEvent:
        """
        Generic event logging for custom actions.
        
        Args:
            user_id: The user performing the action
            action: Action name string
            resource_id: ID of affected resource
            ip_address: Client IP address
            success: Whether the action succeeded
            details: Additional context (stored in error_message field)
        """
        # Try to match to known action, fall back to KEY_UPDATED for key ops
        try:
            audit_action = AuditAction(action)
        except ValueError:
            # Use KEY_UPDATED as fallback for key-related ops
            if "key" in action.lower():
                audit_action = AuditAction.KEY_UPDATED
            else:
                audit_action = AuditAction.MEMORY_STORE  # Generic fallback
        
        return await self.log(
            user_id=user_id,
            action=audit_action,
            resource_id=resource_id,
            ip_address=ip_address,
            success=success,
            error_message=str(details) if details else None,
        )
    
    async def log_auth_failed(
        self,
        user_id: str = "unknown",
        ip_address: str | None = None,
        error: str = "Invalid API key",
    ) -> AuditEvent:
        """Log failed authentication attempt."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.AUTH_FAILED,
            ip_address=ip_address,
            success=False,
            error_message=error,
        )
    
    async def log_rate_limited(
        self,
        user_id: str,
        api_key_id: str | None = None,
        ip_address: str | None = None,
    ) -> AuditEvent:
        """Log rate limit event."""
        return await self.log(
            user_id=user_id,
            action=AuditAction.AUTH_RATE_LIMITED,
            api_key_id=api_key_id,
            ip_address=ip_address,
            success=False,
            error_message="Rate limit exceeded",
        )
    
    async def get_recent_events(
        self,
        user_id: str | None = None,
        action: AuditAction | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get recent audit events with optional filters.
        
        Args:
            user_id: Filter by user
            action: Filter by action type
            limit: Maximum events to return
        
        Returns:
            List of audit event dicts
        """
        return await self.db.get_audit_logs(
            user_id=user_id,
            action=action.value if action else None,
            limit=limit,
        )
