"""Plugin management endpoints – /api/v1/plugins.

Register, list, enable/disable, and configure plugins.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from remembra.auth.middleware import CurrentUser
from remembra.core.limiter import limiter
from remembra.plugins.manager import PluginManager

router = APIRouter(prefix="/plugins", tags=["plugins"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_plugin_manager(request: Request) -> PluginManager:
    manager = getattr(request.app.state, "plugin_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Plugin system is not enabled. Set REMEMBRA_ENABLE_PLUGINS=true to enable.",
        )
    return manager


PluginManagerDep = Annotated[PluginManager, Depends(get_plugin_manager)]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PluginInfo(BaseModel):
    name: str
    version: str
    description: str
    author: str
    enabled: bool


class ActivatePluginRequest(BaseModel):
    name: str = Field(..., description="Plugin name from the registry")
    config: dict[str, Any] = Field(default_factory=dict, description="Plugin configuration")


class TogglePluginRequest(BaseModel):
    enabled: bool = Field(..., description="Enable or disable the plugin")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[PluginInfo],
    summary="List active plugins",
)
@limiter.limit("30/minute")
async def list_active_plugins(
    request: Request,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> list[PluginInfo]:
    """List all currently active plugins."""
    return [PluginInfo(**p) for p in plugin_manager.list_plugins()]


@router.get(
    "/registry",
    summary="List available plugins in the registry",
)
@limiter.limit("30/minute")
async def list_registry(
    request: Request,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """List all registered plugin classes (marketplace catalog)."""
    available = plugin_manager.list_registry()
    active_names = {p["name"] for p in plugin_manager.list_plugins()}
    for p in available:
        p["active"] = p["name"] in active_names
    return {"plugins": available, "count": len(available)}


@router.post(
    "/activate",
    response_model=PluginInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Activate a plugin",
)
@limiter.limit("10/minute")
async def activate_plugin(
    request: Request,
    body: ActivatePluginRequest,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> PluginInfo:
    """Activate a plugin from the registry with optional configuration.

    The plugin must be registered in the global registry (built-in
    plugins are auto-registered).
    """
    # Look up plugin class in registry
    registry = {p["name"]: p for p in plugin_manager.list_registry()}
    if body.name not in registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{body.name}' not found in registry",
        )

    # Check if already active
    if plugin_manager.get_plugin(body.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Plugin '{body.name}' is already active",
        )

    # Import and instantiate built-in plugins by name
    plugin_cls = _resolve_plugin_class(body.name)
    if plugin_cls is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot instantiate plugin '{body.name}'",
        )

    try:
        instance = plugin_cls(config=body.config)
        await plugin_manager.register(instance)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin activation failed: {e}",
        )

    return PluginInfo(**instance.to_dict())


@router.delete(
    "/{plugin_name}",
    summary="Deactivate a plugin",
)
@limiter.limit("10/minute")
async def deactivate_plugin(
    request: Request,
    plugin_name: str,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Deactivate and remove a running plugin."""
    removed = await plugin_manager.unregister(plugin_name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not active",
        )
    return {"deactivated": True, "name": plugin_name}


@router.patch(
    "/{plugin_name}",
    summary="Enable or disable a plugin",
)
@limiter.limit("10/minute")
async def toggle_plugin(
    request: Request,
    plugin_name: str,
    body: TogglePluginRequest,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> dict[str, Any]:
    """Enable or disable a plugin without removing it."""
    plugin = plugin_manager.get_plugin(plugin_name)
    if plugin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not active",
        )
    plugin.enabled = body.enabled
    return {"name": plugin_name, "enabled": plugin.enabled}


@router.get(
    "/{plugin_name}",
    response_model=PluginInfo,
    summary="Get plugin details",
)
@limiter.limit("30/minute")
async def get_plugin_detail(
    request: Request,
    plugin_name: str,
    plugin_manager: PluginManagerDep,
    current_user: CurrentUser,
) -> PluginInfo:
    """Get details about an active plugin."""
    plugin = plugin_manager.get_plugin(plugin_name)
    if plugin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{plugin_name}' is not active",
        )
    return PluginInfo(**plugin.to_dict())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_BUILTIN_PLUGINS: dict[str, str] = {
    "slack-notifier": "remembra.plugins.builtin.slack_notifier.SlackNotifierPlugin",
    "auto-tagger": "remembra.plugins.builtin.auto_tagger.AutoTaggerPlugin",
    "recall-logger": "remembra.plugins.builtin.recall_logger.RecallLoggerPlugin",
}


def _resolve_plugin_class(name: str) -> type | None:
    """Resolve a built-in plugin class by name."""
    import importlib

    path = _BUILTIN_PLUGINS.get(name)
    if not path:
        return None

    module_path, class_name = path.rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ImportError, AttributeError):
        return None
