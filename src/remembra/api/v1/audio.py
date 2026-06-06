"""Audio capture endpoints — /api/v1/audio/*."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from remembra.audio_adapter import AudioAdapter
from remembra.auth.middleware import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])

# Single process-wide adapter. Holds active sessions in-memory.
_adapter = AudioAdapter()

# session_id -> owning user_id. Capture is server-side and resource-consuming,
# so every session is bound to the authenticated user that started it and only
# that user may stop/transcribe it.
_session_owners: dict[str, str] = {}


@router.post("/start")
async def start_audio(
    current_user: CurrentUser,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Start audio capture (requires auth). Optional body: { meeting_id }."""
    meeting_id = (body or {}).get("meeting_id")
    try:
        session = _adapter.start(meeting_id=meeting_id)
    except Exception as exc:  # pragma: no cover - env-dependent
        log.exception("Audio start failed")
        raise HTTPException(status_code=500, detail=f"Audio start failed: {exc}") from exc
    _session_owners[session.session_id] = current_user.user_id
    return {"session": _adapter.session_dict(session)}


@router.post("/stop")
async def stop_audio(
    current_user: CurrentUser,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Stop capture and transcribe (requires auth). Body: { session_id, transcribe?: bool }."""
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
