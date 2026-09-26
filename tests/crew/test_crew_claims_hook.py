"""WP-5 startup hook: the claim timers run on the app's task registry and stop with the app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.crew.test_crew_startup import make_app


def test_claims_sweeper_runs_inside_the_app_lifespan(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app):
        rt = app.state.crew_runtime
        assert "crew.claims" in [h.name for h in rt.started]
        assert "crew-claims-sweeper" in app.state.tasks.names()
        task = rt.extras["task:crew-claims-sweeper"]
        assert not task.done()
    assert task.cancelled() or task.done()
