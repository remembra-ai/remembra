"""Audio capture endpoints — /api/v1/audio/*.

Capture records from the server's own microphone, so it only makes sense on a
server you run yourself. In cloud mode (``REMEMBRA_CLOUD_ENABLED=true``, as
Remembra Cloud runs) every route here answers 404 before any credential is
read, as if it did not exist.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from remembra.audio_adapter import AudioAdapter
from remembra.auth.middleware import CurrentUser, require_memory_store
from remembra.config import get_settings

log = logging.getLogger(__name__)


async def _self_hosted_only() -> None:
    """404 on a server in cloud mode: the hosted service has no microphone of the user's to record."""
    if get_settings().cloud_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(prefix="/audio", tags=["audio"], dependencies=[Depends(_self_hosted_only)])

# Single process-wide adapter. Holds active sessions in-memory.
_adapter = AudioAdapter()

# session_id -> owning user_id. Capture is server-side and resource-consuming,
# so every session is bound to the authenticated user that started it and only
# that user may stop/transcribe it.
_session_owners: dict[str, str] = {}


@router.post("/start", dependencies=[require_memory_store()])
async def start_audio(
    current_user: CurrentUser,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Start audio capture (needs ``memory:store``). Optional body: { meeting_id }."""
    meeting_id = (body or {}).get("meeting_id")
    try:
        session = _adapter.start(meeting_id=meeting_id)
    except Exception as exc:  # pragma: no cover - env-dependent
        log.exception("Audio start failed")
        raise HTTPException(status_code=500, detail=f"Audio start failed: {exc}") from exc
    _session_owners[session.session_id] = current_user.user_id
    return {"session": _adapter.session_dict(session)}


@router.post("/stop", dependencies=[require_memory_store()])
async def stop_audio(
    current_user: CurrentUser,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Stop capture and transcribe (needs ``memory:store``). Body: { session_id, transcribe?: bool }."""
    session_id = (body or {}).get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="'session_id' is required")

    # Enforce ownership: only the user who started the session may stop it.
    # Unknown sessions return 404 (no enumeration of others' session ids).
    owner = _session_owners.get(session_id)
    if owner is None or owner != current_user.user_id:
        raise HTTPException(status_code=404, detail=f"Unknown session_id: {session_id}")

    transcribe = bool(body.get("transcribe", True))

    try:
        session = _adapter.stop(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        log.exception("Audio stop failed")
        raise HTTPException(status_code=500, detail=f"Audio stop failed: {exc}") from exc
    finally:
        _session_owners.pop(session_id, None)

    payload: dict[str, Any] = {"session": _adapter.session_dict(session)}

    if transcribe and session.file_path:
        try:
            segments = _adapter.transcribe(session)
            payload["segments"] = [
                {
                    "start": s.start,
                    "end": s.end,
                    "speaker": s.speaker,
                    "text": s.text,
                    "confidence": s.confidence,
                }
                for s in segments
            ]
            payload["memories"] = _adapter.segments_as_memories(segments, meeting_id=session.meeting_id)
        except RuntimeError as exc:
            payload["transcription_error"] = str(exc)
        except Exception as exc:  # pragma: no cover
            log.exception("Transcription failed")
            payload["transcription_error"] = str(exc)

    return payload
