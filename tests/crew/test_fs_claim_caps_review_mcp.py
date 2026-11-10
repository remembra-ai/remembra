"""F4 follow-up, MCP side, over the live HTTP harness: directory claims use one slot each, and the
refusal a long-path session gets fits the published error contract on the wire."""

from __future__ import annotations

import httpx

from remembra.crew import schemas as S
from tests.crew.test_fs_claim_caps_mcp import _clock, _codex, _live  # noqa: F401  (fixtures)
from tests.crew.test_mcp_crew import agent, call, project_name, use


def test_one_request_for_eight_top_level_directories_is_capped_at_three(live, clock):
    project = project_name("dirs")
    a, sign = _codex(live, project, 1)
    dirs = ["src", "lib", "app", "docs", "tests", "scripts", "public", "db"]
    got = call("crew_claim", paths=dirs)
    assert got.count("GRANTED path ") == 3 and got.count("REFUSED: path ") == 5, got
    assert "Per-session claim limit reached: you hold 3 of 3 exclusive areas (path src, path lib, path app)" in got
    # another agent is not locked out of the rest of the repository
    other = agent(live, "claude-code", f"{project}-other", project)
    use(other)
    got = call("crew_claim", paths=["docs/readme.md", "tests/unit/test_x.py", "src/deep/new_file.py"])
    assert "GRANTED path docs/readme.md" in got and "GRANTED path tests/unit/test_x.py" in got, got
    assert f"REFUSED: path src/deep/new_file.py held EXCLUSIVELY by {sign}" in got, got


def test_the_refusal_on_the_wire_fits_the_error_contract(live, clock):
    project = project_name("wire")
    client, _ = _codex(live, project, 1)
    base = ["apps/storefront/components/checkout", "apps/storefront/components/catalog", "packages/design-system/tokens"]
    paths = [f"{base[0]}/file-number-{i}.component.tsx" for i in range(4)] + [f"{base[1]}/a.tsx", f"{base[2]}/b.ts"]
    assert call("crew_claim", paths=paths).count("GRANTED path ") == 6
    seen: list[httpx.Response] = []
    real = httpx.Client.request

    def spy(self, *args, **kwargs):
        resp = real(self, *args, **kwargs)
        seen.append(resp)
        return resp

    httpx.Client.request = spy  # type: ignore[method-assign]
    try:
        got = call("crew_claim", paths=["zz/x.ts"])
    finally:
        httpx.Client.request = real  # type: ignore[method-assign]
    assert "REFUSED: path zz/x.ts: Per-session claim limit reached: you hold 3 of 3 exclusive areas" in got, got
    refused = [r for r in seen if r.status_code == 409]
    assert len(refused) == 1
    body = refused[0].json()
    body = body.get("detail", body)  # FastAPI wraps the CrewError body
    assert body["error"] == "claim_cap" and len(body["message"]) <= 500
    assert S.validate({k: v for k, v in body.items() if k != "seq"}, S.ERROR_BODY) == []
