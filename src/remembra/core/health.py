"""Health-check helpers."""

from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def check_qdrant(qdrant_url: str) -> dict[str, Any]:
    """Ping Qdrant and return status details.
    
    Note: Internal URLs are not exposed in the response for security.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{qdrant_url}/healthz")
            ok = r.status_code == 200
    except Exception as exc:
        log.warning("qdrant_health_check_failed", error=str(exc))
        ok = False

    # Don't expose internal service URLs - security best practice
    return {"status": "ok" if ok else "degraded"}


def build_health_response(
    version: str,
    qdrant: dict[str, Any],
) -> dict[str, Any]:
    overall = "ok" if qdrant["status"] == "ok" else "degraded"
    return {
        "status": overall,
        "version": version,
        "dependencies": {"qdrant": qdrant},
    }
