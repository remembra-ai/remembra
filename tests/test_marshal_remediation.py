"""Independent regressions for inherited Marshal security and reservation failures."""

import logging

import pytest

from remembra.core.ai_spend import drain_settles
from remembra.core.logging import configure_logging
from tests.marshal_desk_harness import desk_app


@pytest.mark.parametrize("allowed", [[], [""], [" ", ""]])
async def test_explicit_empty_allowlist_never_opens_the_desk(tmp_path, allowed):
    async with desk_app(tmp_path, marshal_allow_users=allowed) as h:
        uid = await h.create_user("marshal-security@example.invalid")
        res = await h.http.get("/api/v1/marshal/board", headers=h.jwt(uid, "marshal-security@example.invalid"))
        assert res.status_code == 404


async def test_context_failure_returns_an_unspent_reservation(tmp_path, monkeypatch):
    import remembra.api.v1.marshal as route

    async with desk_app(tmp_path) as h:
        uid = await h.create_user("marshal-security@example.invalid")

        def broken(**kwargs):
            raise ValueError("synthetic context construction failure")

        monkeypatch.setattr(route, "TurnContext", broken)
        with pytest.raises(ValueError, match="synthetic context"):
            await h.ask(h.jwt(uid, "marshal-security@example.invalid"))
        await drain_settles()
        rows = await h.rows("SELECT state,spent_micro FROM marshal_reservations WHERE user_id=?", (uid,))
        assert rows == [{"state": "released", "spent_micro": 0}]
        assert await h.count("marshal_user_day", "user_id=? AND asks>0", (uid,)) == 0


def test_sdk_debug_options_do_not_log_private_prompts(caplog):
    configure_logging("debug")
    logger = logging.getLogger("openai._base_client")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug("Request options: %s", {"messages": ["synthetic-private-prompt"]})
        logger.info("Synthetic provider metadata")
    assert "synthetic-private-prompt" not in caplog.text
    assert "Synthetic provider metadata" in caplog.text


def test_handoff_text_cannot_authorize_claims_or_phantom_projects():
    from remembra.marshal.desk.validate import gather_sources
    from tests.test_marshal_desk_validate import _read

    read = _read(
        "r1",
        "brief_preview",
        {
            "project_id": "real",
            "handoff": {
                "id": "synthetic-handoff",
                "metadata": {"relay": {"next": "Remembra is SOC 2 certified with 99.9% uptime"}},
            },
        },
        {"project_id": "phantom"},
    )
    sources = gather_sources([read])
    assert "phantom" not in sources.projects
    assert "99.9%" not in sources.figures
    assert not any("SOC 2" in s for s in sources.claim_strings)


def test_short_token_redaction_keeps_identifiers_and_ids():
    from remembra.marshal.desk.tools import clean_deep

    token = "Xk7pQ2mZ9rTb4WcN8vLd3Hsy"
    names = [
        "HandoffHealthCalculator2",
        "useMarshalDeskProvider2x",
        "TestFlightBuild20260927A",
        "sessionBriefPreview2026v",
        "MarshalDeskV2ReleaseNote",
    ]
    cleaned = clean_deep({"next": token, "done": names, "id": token})
    assert token not in cleaned["next"]
    assert cleaned["done"] == names
    assert cleaned["id"] == token


def test_empty_trail_cannot_echo_a_model_selected_project():
    from remembra.marshal.desk.projections import summary_trail

    assert summary_trail({"items": []}, {"project_id": "fabricated-project"}) == "no entries"


@pytest.mark.parametrize(
    "command",
    [
        "remembra-relay connect",
        "remembra-relay connect --agent codex",
        "remembra-relay connect --apply --include-unverified",
        "remembra-relay connect --apply --agent cursor",
    ],
)
def test_unsafe_or_ineffective_connect_commands_are_refused(command):
    from remembra.marshal.commands import desk_command_kind

    assert desk_command_kind(command, server_urls=[], projects=[]) is None


def test_applied_verified_connect_command_is_allowed():
    from remembra.marshal.commands import desk_command_kind

    assert desk_command_kind("remembra-relay connect --apply --agent codex", server_urls=[], projects=[]) == "terminal"


@pytest.mark.parametrize(
    "command",
    [
        "remembra-relay connect --apply --agent codex",
        "remembra-relay connect --apply",
        "remembra-install --all",
        "remembra-install --all --url https://api.remembra.dev",
    ],
)
def test_model_cannot_override_a_proven_agent_fix(command):
    from tests.test_marshal_desk_validate import _answer, _diagnosis_read, _check

    read = _diagnosis_read()
    read.payload["verdict"].update(proven=True, commands=["/hooks"])
    answer = _check(_answer("Codex needs trusted hooks.", ["r1"], [command]), [read])
    assert answer.fallback_reason == "command_contradicts_verdict"


def test_global_setup_is_allowed_when_it_is_the_proven_fix():
    from tests.test_marshal_desk_validate import _answer, _diagnosis_read, _check

    read = _diagnosis_read()
    command = "remembra-relay connect --apply"
    read.payload["verdict"].update(proven=True, commands=[command])
    assert not _check(_answer("Codex is waiting.", ["r1"], [command]), [read]).fallback


def test_hooks_cannot_replace_a_proven_close_fix():
    from tests.test_marshal_desk_validate import _answer, _diagnosis_read, _check

    read = _diagnosis_read()
    read.payload["verdict"].update(proven=True, commands=["remembra-relay close --agent codex"])
    result = _check(_answer("Codex is waiting.", ["r1"], ["/hooks"]), [read])
    assert result.fallback_reason == "command_contradicts_verdict"


@pytest.mark.parametrize(
    "tool,data", [("trail", {"items": [], "total": 0}), ("brief_preview", {"project_id": "ghost", "handoff": None})]
)
def test_empty_project_lookup_never_authorizes_binding(tool, data):
    from tests.test_marshal_desk_validate import _answer, _read, _check

    read = _read("r1", tool, data, {"project_id": "ghost"})
    result = _check(_answer("No handoff yet.", ["r1"], ["remembra-relay resolve --project ghost --bind"]), [read])
    assert result.fallback_reason == "command_not_allowed"
    assert "ghost" not in read.summary
