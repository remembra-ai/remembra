"""Aggregate all versioned routers."""

from fastapi import APIRouter

from remembra.api.v1 import (
    admin,
    audio,
    auth,
    billing,
    brain,
    cloud,
    conflicts,
    debug,
    embeddings,
    entities,
    inbox,
    ingest,
    keys,
    meetings,
    memories,
    plugins,
    spaces,
    teams,
    temporal,
    transfer,
    users,
    webhooks,
    websocket,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router, prefix="/v1")
api_router.include_router(billing.router, prefix="/v1")
api_router.include_router(memories.router, prefix="/v1")
api_router.include_router(keys.router, prefix="/v1")
api_router.include_router(ingest.router, prefix="/v1")
api_router.include_router(temporal.router, prefix="/v1")
api_router.include_router(entities.router, prefix="/v1")
api_router.include_router(users.router, prefix="/v1")
api_router.include_router(cloud.router, prefix="/v1")
api_router.include_router(debug.router, prefix="/v1")
api_router.include_router(webhooks.router, prefix="/v1")
api_router.include_router(conflicts.router, prefix="/v1")
api_router.include_router(admin.router, prefix="/v1")
api_router.include_router(transfer.router, prefix="/v1")
api_router.include_router(spaces.router, prefix="/v1")
api_router.include_router(teams.router, prefix="/v1")
api_router.include_router(embeddings.router, prefix="/v1")
api_router.include_router(plugins.router, prefix="/v1")
api_router.include_router(websocket.router, prefix="/v1")
api_router.include_router(meetings.router, prefix="/v1")
api_router.include_router(audio.router, prefix="/v1")
api_router.include_router(inbox.router, prefix="/v1")
api_router.include_router(brain.router, prefix="/v1")
