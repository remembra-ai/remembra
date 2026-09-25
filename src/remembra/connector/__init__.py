"""Remote MCP connector for the Claude apps (web, desktop, mobile) and ChatGPT.

Enable with ``REMEMBRA_CONNECTOR_ENABLED=true`` and ``REMEMBRA_PUBLIC_URL``.
:func:`install_connector` adds the OAuth routes and the ``/mcp`` endpoint to
the API app; :func:`connector_lifespan` creates the OAuth tables and runs
the MCP session manager for the app's lifetime.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from starlette.routing import Route

from remembra.config import Settings


def install_connector(app: FastAPI, settings: Settings) -> None:
    """Register the connector's routes on ``app`` (call before any catch-all route)."""
    if not settings.connector_enabled:
        return
    if not settings.auth_enabled:
        raise RuntimeError("REMEMBRA_CONNECTOR_ENABLED requires authentication (REMEMBRA_AUTH_ENABLED=true).")

    try:
        import mcp  # noqa: F401
    except ImportError as e:  # the API image must be built with the "mcp" extra
        raise RuntimeError("REMEMBRA_CONNECTOR_ENABLED requires the MCP SDK: install remembra[server,mcp].") from e

    from remembra.connector.mcp_app import ConnectorEndpoint, build_connector_mcp
    from remembra.connector.oauth import MCP_PATH, connections_router, router

    endpoint = ConnectorEndpoint(build_connector_mcp())
    app.state.connector_endpoint = endpoint
    app.include_router(router)
    app.include_router(connections_router, prefix="/api/v1")
    # A Route (not a Mount) so POST /mcp is served without a slash redirect.
    app.router.routes.append(Route(MCP_PATH, endpoint=endpoint, methods=["GET", "POST", "DELETE"]))


@contextlib.asynccontextmanager
async def connector_lifespan(app: Any, settings: Settings) -> AsyncIterator[None]:
    """Create the OAuth store on ``app.state.db`` and run the MCP session manager."""
    endpoint = getattr(app.state, "connector_endpoint", None)
    if not settings.connector_enabled or endpoint is None:
        yield
        return

    from remembra.connector.store import ConnectorStore, successor_key

    store = ConnectorStore(
        app.state.db,
        rotation_key=successor_key(settings.jwt_secret),
        access_ttl_seconds=settings.connector_access_token_ttl_seconds,
        refresh_ttl_seconds=settings.connector_refresh_token_ttl_days * 86400,
    )
    await store.init_schema()
    await store.prune()
    app.state.connector_store = store
    async with endpoint.lifespan():
        yield
