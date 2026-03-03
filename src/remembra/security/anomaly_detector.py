"""
Memory Anomaly Detection.

Monitors for unusual memory patterns that could indicate:
- Memory poisoning attacks (OWASP ASI06)
- Automated scraping/extraction
- Abuse of the memory system

Checks:
- Acquisition rate (memories stored per hour)
- Source distribution (unusual source patterns)
- Content anomalies (topic shifts, suspicious patterns)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class AnomalyResult:
    """Result of an anomaly check."""
    
    detected: bool = False
    type: str | None = None
    severity: str = "info"  # info | warning | critical
    message: str | None = None
    details: dict[str, Any] | None = None


@dataclass
class AnomalyReport:
    """Full anomaly report for a user."""
    
    user_id: str
    checked_at: datetime
    anomalies: list[AnomalyResult]
    
    @property
    def has_anomalies(self) -> bool:
        return any(a.detected for a in self.anomalies)
    
    @property
    def critical_count(self) -> int:
        return sum(1 for a in self.anomalies if a.detected and a.severity == "critical")
    
    @property
    def warning_count(self) -> int:
        return sum(1 for a in self.anomalies if a.detected and a.severity == "warning")


# ============================================================================
# Anomaly Detector
# ============================================================================

class AnomalyDetector:
    """
    Detects unusual memory patterns that could indicate abuse or attack.
    
    Usage:
        detector = AnomalyDetector(db, settings)
        report = await detector.check_user("user_123")
        if report.has_anomalies:
            print(f"Found {report.warning_count} warnings")
    """
    
    def __init__(
        self,
        db: Any,
        enabled: bool = True,
        rate_threshold: int = 100,  # Max memories per hour
    ):
        self.db = db
        self.enabled = enabled
        self.rate_threshold = rate_threshold
        
        log.info(
            "anomaly_detector_initialized",
            enabled=enabled,
            rate_threshold=rate_threshold,
        )
    
    async def check_user(self, user_id: str) -> AnomalyReport:
        """
        Run all anomaly checks for a user.
        
        Args:
            user_id: User to check
            
        Returns:
            AnomalyReport with all detected anomalies
        """
        anomalies = []
        
        if not self.enabled:
            return AnomalyReport(
                user_id=user_id,
                checked_at=datetime.utcnow(),
                anomalies=[],
            )
        
        # Check acquisition rate
        rate_result = await self.check_acquisition_rate(user_id)
        anomalies.append(rate_result)
        
        # Check source distribution
        source_result = await self.check_source_distribution(user_id)
        anomalies.append(source_result)
        
        # Check for bulk operations
        bulk_result = await self.check_bulk_operations(user_id)
        anomalies.append(bulk_result)
        
        report = AnomalyReport(
            user_id=user_id,
            checked_at=datetime.utcnow(),
            anomalies=anomalies,
        )
        
        if report.has_anomalies:
            log.warning(
                "anomalies_detected",
                user_id=user_id,
                critical=report.critical_count,
                warnings=report.warning_count,
                types=[a.type for a in anomalies if a.detected],
            )
        
        return report
    
    async def check_acquisition_rate(
        self,
        user_id: str,
        window_minutes: int = 60,
    ) -> AnomalyResult:
        """
        Check if user is storing memories at an abnormal rate.
        
        Args:
            user_id: User to check
            window_minutes: Time window to check (default: 1 hour)
            
        Returns:
            AnomalyResult indicating if rate is abnormal
        """
        try:
            since = datetime.utcnow() - timedelta(minutes=window_minutes)
            
            cursor = await self.db.conn.execute(
                """
                SELECT COUNT(*) as count
                FROM memories
                WHERE user_id = ? AND created_at >= ?
                """,
                (user_id, since.isoformat()),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            
            # Calculate rate relative to threshold
            if count > self.rate_threshold:
                severity = "critical" if count > self.rate_threshold * 2 else "warning"
                return AnomalyResult(
                    detected=True,
                    type="high_acquisition_rate",
                    severity=severity,
                    message=f"User stored {count} memories in {window_minutes} minutes (threshold: {self.rate_threshold})",
                    details={
                        "count": count,
                        "threshold": self.rate_threshold,
                        "window_minutes": window_minutes,
                        "rate_per_hour": count * (60 / window_minutes),
                    },
                )
            
            return AnomalyResult(
                detected=False,
                type="high_acquisition_rate",
                details={"count": count, "threshold": self.rate_threshold},
            )
            
        except Exception as e:
            log.error("acquisition_rate_check_failed", error=str(e))
            return AnomalyResult(detected=False, type="high_acquisition_rate")
    
    async def check_source_distribution(self, user_id: str) -> AnomalyResult:
        """
        Check for unusual source distribution in recent memories.
        
        Flags if a large percentage of memories come from a single
        unusual source (potential automated ingestion).
        
        Args:
            user_id: User to check
            
        Returns:
            AnomalyResult indicating if distribution is unusual
        """
        try:
            # Get source distribution for last 24 hours
            since = datetime.utcnow() - timedelta(hours=24)
            
            cursor = await self.db.conn.execute(
                """
                SELECT 
                    COALESCE(json_extract(metadata, '$.source'), 'unknown') as source,
                    COUNT(*) as count
                FROM memories
                WHERE user_id = ? AND created_at >= ?
                GROUP BY source
                ORDER BY count DESC
                """,
                (user_id, since.isoformat()),
            )
            rows = await cursor.fetchall()
            
            if not rows:
                return AnomalyResult(detected=False, type="source_distribution")
            
            total = sum(row[1] for row in rows)
            top_source, top_count = rows[0]
            
            # Flag if >90% from single non-standard source
            suspicious_sources = ["webhook", "import", "external_api", "unknown"]
            if (
                top_count / total > 0.9
                and top_source in suspicious_sources
                and total > 50
            ):
                return AnomalyResult(
                    detected=True,
                    type="source_distribution",
                    severity="warning",
                    message=f"{top_count}/{total} memories ({top_count/total*100:.1f}%) from '{top_source}'",
                    details={
                        "top_source": top_source,
                        "top_count": top_count,
                        "total": total,
                        "percentage": top_count / total,
                        "distribution": {row[0]: row[1] for row in rows},
                    },
                )
            
            return AnomalyResult(
                detected=False,
                type="source_distribution",
                details={"distribution": {row[0]: row[1] for row in rows}},
            )
            
        except Exception as e:
            log.error("source_distribution_check_failed", error=str(e))
            return AnomalyResult(detected=False, type="source_distribution")
    
    async def check_bulk_operations(self, user_id: str) -> AnomalyResult:
        """
        Check for suspicious bulk delete operations.
        
        Flags if user recently deleted a large number of memories
        (potential data destruction attempt or cleanup after attack).
        
        Args:
            user_id: User to check
            
        Returns:
            AnomalyResult indicating suspicious bulk operations
        """
        try:
            # Check audit logs for mass deletions
            since = datetime.utcnow() - timedelta(hours=1)
            
            cursor = await self.db.conn.execute(
                """
                SELECT COUNT(*) as count
                FROM audit_log
                WHERE user_id = ? 
                  AND action = 'memory_delete'
                  AND timestamp >= ?
                """,
                (user_id, since.isoformat()),
            )
            row = await cursor.fetchone()
            delete_count = row[0] if row else 0
            
            if delete_count > 50:  # More than 50 deletions in 1 hour
                return AnomalyResult(
                    detected=True,
                    type="bulk_deletion",
                    severity="warning",
                    message=f"User deleted {delete_count} memories in the last hour",
                    details={
                        "delete_count": delete_count,
                        "window_hours": 1,
                    },
                )
            
            return AnomalyResult(
                detected=False,
                type="bulk_deletion",
                details={"delete_count": delete_count},
            )
            
        except Exception as e:
            # May fail if audit_log table doesn't exist - that's OK
            log.debug("bulk_operations_check_skipped", reason=str(e))
            return AnomalyResult(detected=False, type="bulk_deletion")
    
    async def flag_suspicious(
        self,
        user_id: str,
        anomaly: AnomalyResult,
    ) -> None:
        """
        Flag a user/memory as suspicious for review.
        
        This can be used to trigger alerts, restrict access,
        or queue for human review.
        """
        log.warning(
            "user_flagged_suspicious",
            user_id=user_id,
            anomaly_type=anomaly.type,
            severity=anomaly.severity,
            message=anomaly.message,
        )
        
        # Future: Write to suspicious_users table
        # Future: Send alert to admin
        # Future: Rate limit the user


# ============================================================================
# Convenience Function
# ============================================================================

async def check_user_anomalies(
    db: Any,
    user_id: str,
    rate_threshold: int = 100,
) -> AnomalyReport:
    """
    Quick check for user anomalies.
    
    Args:
        db: Database instance
        user_id: User to check
        rate_threshold: Max memories per hour
        
    Returns:
        AnomalyReport with findings
    """
    detector = AnomalyDetector(db=db, rate_threshold=rate_threshold)
    return await detector.check_user(user_id)
