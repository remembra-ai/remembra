"""The per-account opt-out (``GET``/``PUT /api/v1/marshal/settings``, table ``marshal_prefs``).

On by default; off refuses the board and asks (403 ``marshal_opted_out``)
while the settings stay reachable, so the account can turn it back on. Only a
dashboard login can change it: API keys and the desk's own principal can't.
The table's rows go with the account on erasure (``test_account_erasure.py``
enforces that every table is registered).
"""

from __future__ import annotations

from remembra.account.erasure import ERASURE_RULES, EXEMPT_TABLES
from remembra.auth.middleware import AuthenticatedUser, connector_principal
from tests.marshal_desk_harness import desk_app


async def test_the_desk_is_on_until_the_account_turns_it_off_and_back_on(tmp_path) -> None:
    async with desk_app(tmp_path, openai_api_key=None) as h:
        uid = await h.create_user("prefs@example.com")
        jwt = h.jwt(uid, "prefs@example.com")
        assert (await h.http.get("/api/v1/marshal/settings", headers=jwt)).json() == {"desk": True}
        assert await h.count("marshal_prefs") == 0  # reading never writes

        off = await h.http.put("/api/v1/marshal/settings", json={"desk": False}, headers=jwt)
        assert (off.status_code, off.json()) == (200, {"desk": False})
        assert (await h.http.get("/api/v1/marshal/board", headers=jwt)).status_code == 403
        assert (await h.ask(jwt)).json()["detail"]["error"] == "marshal_opted_out"
        assert (await h.http.get("/api/v1/marshal/settings", headers=jwt)).json() == {"desk": False}

        on = await h.http.put("/api/v1/marshal/settings", json={"desk": True}, headers=jwt)
        assert on.json() == {"desk": True}
        assert (await h.http.get("/api/v1/marshal/board", headers=jwt)).status_code == 200
        assert (await h.ask(jwt)).json()["detail"]["reason"] == "no_key"  # past the opt-out, stopped by the missing key
        assert await h.rows("SELECT user_id, desk FROM marshal_prefs") == [{"user_id": uid, "desk": 1}]

        # One account's choice is its own.
        other = await h.create_user("other@example.com")
        assert (await h.http.get("/api/v1/marshal/settings", headers=h.jwt(other, "other@example.com"))).json() == {"desk": True}


async def test_only_a_dashboard_login_changes_it(tmp_path) -> None:
    async with desk_app(tmp_path) as h:
        uid = await h.create_user("who@example.com")
        jwt = h.jwt(uid, "who@example.com")
        key = await h.api_key(uid)
        res = await h.http.put("/api/v1/marshal/settings", json={"desk": False}, headers=key)
        assert res.status_code == 403 and res.json()["detail"]["error"] == "marshal_login_required"
        marshal = AuthenticatedUser(
            user_id=uid, api_key_id="marshal:0123456789ab", rate_limit_tier="standard", scopes=["memory:recall"]
        )
        with connector_principal(marshal):
            res = await h.http.put("/api/v1/marshal/settings", json={"desk": False})
        assert res.status_code == 403 and res.json()["detail"]["error"] == "delegated_principal_refused"
        for bad in ({"desk": "no"}, {"desk": 0}, {}, {"desk": False, "extra": 1}):
            assert (await h.http.put("/api/v1/marshal/settings", json=bad, headers=jwt)).status_code == 422, bad
        assert await h.count("marshal_prefs") == 0


async def test_disabled_desk_settings_are_404(tmp_path) -> None:
    async with desk_app(tmp_path, marshal_enabled=False) as h:
        uid = await h.create_user("off@example.com")
        res = await h.http.get("/api/v1/marshal/settings", headers=h.jwt(uid, "off@example.com"))
        assert res.status_code == 404 and res.json()["detail"]["error"] == "marshal_unavailable"


def test_the_desk_tables_are_registered_for_erasure() -> None:
    by_user = {rule.table for rule in ERASURE_RULES}
    assert {"marshal_user_day", "marshal_reservations", "marshal_prefs"} <= by_user
    assert "marshal_budget" in EXEMPT_TABLES and "marshal_budget" not in by_user
