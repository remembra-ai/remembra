"""WebSocket endpoint for real-time memory updates."""

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from remembra.config import get_settings

log = structlog.get_logger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""
    
    def __init__(self) -> None:
        # Map: namespace -> set of (websocket, project_id)
        self._connections: dict[str, set[tuple[WebSocket, str | None]]] = {}
        self._lock = asyncio.Lock()
    
    async def connect(
        self,
        websocket: WebSocket,
        namespace: str = "default",
        project_id: str | None = None,
    ) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            if namespace not in self._connections:
                self._connections[namespace] = set()
            self._connections[namespace].add((websocket, project_id))
        log.info(
            "websocket_connected",
            namespace=namespace,
            project_id=project_id,
            total_connections=self._count_all(),
        )
    
    async def disconnect(
        self,
        websocket: WebSocket,
        namespace: str = "default",
        project_id: str | None = None,
    ) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if namespace in self._connections:
                self._connections[namespace].discard((websocket, project_id))
                if not self._connections[namespace]:
                    del self._connections[namespace]
        log.info(
            "websocket_disconnected",
            namespace=namespace,
            project_id=project_id,
            total_connections=self._count_all(),
        )
    
    async def broadcast(
        self,
        event_type: str,
        data: dict[str, Any],
        namespace: str = "default",
        project_id: str | None = None,
    ) -> int:
        """
        Broadcast an event to all connected clients in a namespace.
        
        If project_id is provided, only sends to clients subscribed to that project.
        Returns the number of clients that received the message.
        """
        message = json.dumps({
            "type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "namespace": namespace,
            "project_id": project_id,
        })
        
        sent_count = 0
        disconnected: list[tuple[WebSocket, str | None]] = []
        
        async with self._lock:
            connections = self._connections.get(namespace, set()).copy()
        
        for ws, ws_project_id in connections:
            # Filter by project_id if specified
            if project_id and ws_project_id and ws_project_id != project_id:
                continue
            
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(message)
                    sent_count += 1
            except Exception as e:
                log.warning("websocket_send_failed", error=str(e))
                disconnected.append((ws, ws_project_id))
        
        # Clean up disconnected clients
        if disconnected:
            async with self._lock:
                for item in disconnected:
                    if namespace in self._connections:
                        self._connections[namespace].discard(item)
        
        return sent_count
    
    def _count_all(self) -> int:
        """Count total active connections across all namespaces."""
        return sum(len(conns) for conns in self._connections.values())
    
    async def get_stats(self) -> dict[str, Any]:
        """Get connection statistics."""
        async with self._lock:
            return {
                "total_connections": self._count_all(),
                "namespaces": {
                    ns: len(conns) for ns, conns in self._connections.items()
                },
            }


# Global connection manager instance
connection_manager = ConnectionManager()


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager (for dependency injection)."""
    return connection_manager


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    namespace: str = Query("default", description="Namespace to subscribe to"),
    project_id: str | None = Query(None, description="Optional project ID filter"),
    api_key: str | None = Query(None, description="API key for authentication"),
) -> None:
    """
    WebSocket endpoint for real-time memory updates.
    
    Clients receive events when memories are:
    - Stored (memory.created)
    - Updated (memory.updated)  
    - Deleted (memory.deleted)
    - Searched (memory.searched) - optional
    
    Query params:
    - namespace: Subscribe to a specific namespace (default: "default")
    - project_id: Filter events to a specific project
    - api_key: API key for authentication (optional based on settings)
    """
    settings = get_settings()
    
    # Validate API key if auth is enabled
    if settings.auth_enabled and not settings.debug and not api_key:
        await websocket.close(code=4001, reason="API key required")
        return
        # Note: Full validation would require async DB lookup
        # For MVP, we accept presence of key; production should validate
    
    await connection_manager.connect(websocket, namespace, project_id)
    
    try:
        # Send initial connection confirmation
        await websocket.send_json({
            "type": "connected",
            "data": {
                "namespace": namespace,
                "project_id": project_id,
                "message": "Connected to Remembra real-time updates",
            },
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages (ping/pong, or future commands)
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=60.0  # Ping every 60s to detect disconnects
                )
                
                # Handle ping
                if data == "ping":
                    await websocket.send_text("pong")
                    continue
                
                # Handle subscription changes
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "subscribe":
                        # Client wants to change subscription
                        new_ns = msg.get("namespace", namespace)
                        new_proj = msg.get("project_id", project_id)
                        
                        # Update subscription
                        await connection_manager.disconnect(websocket, namespace, project_id)
                        namespace = new_ns
                        project_id = new_proj
                        await connection_manager.connect(websocket, namespace, project_id)
                        
                        await websocket.send_json({
                            "type": "subscribed",
                            "data": {"namespace": namespace, "project_id": project_id},
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                        })
                except json.JSONDecodeError:
                    pass  # Ignore invalid JSON
                    
            except TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text("ping")
                except Exception:
                    break
                    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("websocket_error", error=str(e))
    finally:
        await connection_manager.disconnect(websocket, namespace, project_id)


@router.get("/ws/stats", tags=["websocket"])
async def websocket_stats():
    """Get WebSocket connection statistics."""
    return await connection_manager.get_stats()
