"""Audio capture is off on the hosted service (owner decision 3, 2026-09-27).

``POST /api/v1/audio/start`` and ``/stop`` record from the server's own
microphone, which on a hosted server is nobody's microphone. On a server in
cloud mode (``REMEMBRA_CLOUD_ENABLED=true``, the switch Remembra Cloud runs
with) both answer 404, before any credential is read, like a route that does
not exist. A self-hosted server keeps them, behind ``memory:store``.

The adapter is replaced by a fake so no test opens a real microphone.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pytest

from remembra.api.v1 import audio
from tests.security_harness import make_settings, secure_app

ROUTES = (("/api/v1/audio/start", {}), ("/api/v1/audio/stop", {"session_id": "s-1"}))


@dataclass
class _FakeSession:
    session_id: str
    meeting_id: str | None = None
    file_path: str | None = None


@dataclass
class _FakeAdapter:
    started: list[str | None] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def start(self, meeting_id: str | None = None) -> _FakeSession:
        self.started.append(meeting_id)
        return _FakeSession(session_id=f"s-{len(self.started)}", meeting_id=meeting_id)

    def stop(self, session_id: str) -> _FakeSession:
        self.stopped.append(session_id)
        return _FakeSession(session_id=session_id)

    def session_dict(self, session: _FakeSession) -> dict[str, Any]:
        return asdict(session)


@pytest.fixture()
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> _FakeAdapter:
    fake = _FakeAdapter()
    monkeypatch.setattr(audio, "_adapter", fake)
    monkeypatch.setattr(audio, "_session_owners", {})
    return fake


async def test_audio_routes_are_not_found_in_cloud_mode(tmp_path, fake_adapter: _FakeAdapter) -> None:
    async with secure_app(tmp_path, [audio.router], settings=make_settings(cloud_enabled=True)) as h:
        uid = await h.create_user("cloud-audio@example.com")
        editor, _ = await h.api_key(uid, "editor")
        viewer, _ = await h.api_key(uid, "viewer")
        for path, body in ROUTES:
            for headers in ({"X-API-Key": editor}, {"X-API-Key": viewer}, h.jwt(uid), {}):
                r = await h.client.post(path, json=body, headers=headers)
                assert r.status_code == 404, (path, headers, r.text)
                assert r.json() == {"detail": "Not Found"}
    assert fake_adapter.started == []
    assert fake_adapter.stopped == []


async def test_self_hosted_server_keeps_audio_behind_memory_store(tmp_path, fake_adapter: _FakeAdapter) -> None:
    async with secure_app(tmp_path, [audio.router], settings=make_settings(cloud_enabled=False)) as h:
        uid = await h.create_user("self-hosted-audio@example.com")
        editor, _ = await h.api_key(uid, "editor")
        viewer, _ = await h.api_key(uid, "viewer")

        assert (await h.client.post("/api/v1/audio/start", json={})).status_code == 401
        r = await h.client.post("/api/v1/audio/start", json={}, headers={"X-API-Key": viewer})
        assert r.status_code == 403, r.text
        assert fake_adapter.started == []

        r = await h.client.post("/api/v1/audio/start", json={"meeting_id": "m-1"}, headers={"X-API-Key": editor})
        assert r.status_code == 200, r.text
        session_id = r.json()["session"]["session_id"]
        assert fake_adapter.started == ["m-1"]

        r = await h.client.post(
            "/api/v1/audio/stop", json={"session_id": session_id, "transcribe": False}, headers={"X-API-Key": editor}
        )
        assert r.status_code == 200, r.text
        assert fake_adapter.stopped == [session_id]
