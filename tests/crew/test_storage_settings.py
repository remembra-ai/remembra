"""WP-1: crews.settings defaults, validation and patching (spec §3.2)."""

from __future__ import annotations

import json

import pytest

from remembra.crew import settings as cs
from remembra.crew.settings import (
    DEFAULT_SETTINGS,
    SettingsError,
    apply_settings_patch,
    changed_keys,
    default_settings,
    dumps_settings,
    load_settings,
    validate_settings,
)

SPEC_DEFAULTS = json.loads(
    """{"enforcement":"enforce","lease_ttl_s":600,"idle_park_s":3600,"reserve_ttl_s":86400,
 "task_baton_expiry":"never","host_lost_after_s":1800,
 "auto_claim":true,"auto_claim_leaf_only":true,"max_exclusive_claims_per_session":3,"max_exclusive_claims_per_agent":6,
 "undeclared_policy":"footprint","no_zone_bootstrap":"suggested_enforce",
 "auto_adopt":"same_checkout","adopt_on_first_write":false,"baton_push":"auto",
 "require_report_for_done":true,"strict_reports":true,
 "deploy_gate":{"require_pushed":true,"require_live_check":false},"live_check_domains":[],
 "wip_per_session":1,"inject":{"turn_max_chars":600,"pretool_max_chars":300,"min_interval_s":60,"compact_after":40},
 "checkpoint":{"interval_s":600,"calls":40},
 "fail_closed_zones":[],"interactive_override":false,"require_verified_agents_for_claims":false,
 "readonly_fence_for_advisory":true,"notify":{"realtime":["email"]}}"""
)


def test_defaults_are_exactly_the_spec_defaults_and_valid() -> None:
    assert dict(DEFAULT_SETTINGS) == SPEC_DEFAULTS
    assert validate_settings(default_settings()) == SPEC_DEFAULTS


def test_default_settings_returns_independent_copies() -> None:
    a = default_settings()
    a["inject"]["turn_max_chars"] = 1
    a["live_check_domains"].append("x.com")
    assert default_settings() == SPEC_DEFAULTS


def test_require_report_for_done_can_never_be_false() -> None:
    with pytest.raises(SettingsError) as exc:
        apply_settings_patch(default_settings(), {"require_report_for_done": False})
    assert "require_report_for_done" in exc.value.errors
    with pytest.raises(SettingsError):
        apply_settings_patch(default_settings(), {"require_report_for_done": 0})


@pytest.mark.parametrize(
    ("patch", "key"),
    [
        ({"enforcement": "off-ish"}, "enforcement"),
        ({"enforcement": None, "bogus": 1}, "bogus"),
        ({"lease_ttl_s": 60}, "lease_ttl_s"),  # would leave no room for the 60 s fence
        ({"lease_ttl_s": "600"}, "lease_ttl_s"),
        ({"lease_ttl_s": True}, "lease_ttl_s"),  # bool is not an int here
        ({"auto_claim": 1}, "auto_claim"),  # int is not a bool here
        ({"undeclared_policy": "claim_everything"}, "undeclared_policy"),
        ({"task_baton_expiry": 3600}, "task_baton_expiry"),
        ({"task_baton_expiry": "sometimes"}, "task_baton_expiry"),
        ({"max_exclusive_claims_per_session": 0}, "max_exclusive_claims_per_session"),
        ({"max_exclusive_claims_per_session": 5, "max_exclusive_claims_per_agent": 4}, "max_exclusive_claims_per_agent"),
        ({"inject": {"turn_max_chars": 601}}, "inject.turn_max_chars"),  # §10.4 per-turn cap
        ({"inject": {"pretool_max_chars": 301}}, "inject.pretool_max_chars"),
        ({"inject": {"surprise": 1}}, "inject.surprise"),
        ({"inject": "loud"}, "inject"),
        ({"checkpoint": {"interval_s": 10}}, "checkpoint.interval_s"),
        ({"notify": {"realtime": ["sms"]}}, "notify.realtime"),
        ({"notify": {"realtime": ["email", "email"]}}, "notify.realtime"),
        ({"fail_closed_zones": ["POS Section"]}, "fail_closed_zones"),
        ({"fail_closed_zones": ["pos", "pos"]}, "fail_closed_zones"),
        ({"deploy_gate": {"require_live_check": True}}, "deploy_gate.require_live_check"),  # needs a domain
        ({"baton_push": "cloud"}, "baton_push"),
        ({"wip_per_session": 9}, "wip_per_session"),
    ],
)
def test_invalid_patches_are_rejected_with_the_key(patch: dict, key: str) -> None:
    with pytest.raises(SettingsError) as exc:
        apply_settings_patch(default_settings(), patch)
    assert key in exc.value.errors, exc.value.errors


@pytest.mark.parametrize(
    "domain",
    [
        "169.254.169.254",
        "10.0.0.1",
        "localhost",
        "coolify.internal",
        "printer.local",
        "https://yaadbooks.com",
        "yaadbooks.com:443",
        "yaadbooks.com/api",
        "YaadBooks.com",
        " yaadbooks.com",
        "yaadbooks.com.",
        "intranet",
        "*.yaadbooks.com",
    ],
)
def test_live_check_domains_must_be_public_host_names(domain: str) -> None:
    with pytest.raises(SettingsError) as exc:
        apply_settings_patch(default_settings(), {"live_check_domains": [domain]})
    assert "live_check_domains" in exc.value.errors


def test_valid_patch_merges_nested_objects_and_null_resets() -> None:
    current = default_settings()
    updated = apply_settings_patch(
        current,
        {
            "enforcement": "observe",
            "inject": {"min_interval_s": 120},
            "live_check_domains": ["yaadbooks.com", "api.yaadbooks.com"],
            "deploy_gate": {"require_live_check": True},
            "fail_closed_zones": ["pos"],
            "task_baton_expiry": 3 * 86400,
            "notify": {"realtime": ["email", "webhook"]},
        },
    )
    assert updated["enforcement"] == "observe"
    assert updated["inject"] == {"turn_max_chars": 600, "pretool_max_chars": 300, "min_interval_s": 120, "compact_after": 40}
    assert updated["deploy_gate"] == {"require_pushed": True, "require_live_check": True}
    assert current == SPEC_DEFAULTS  # the input is not mutated

    reset = apply_settings_patch(updated, {"enforcement": None, "inject": {"min_interval_s": None}})
    assert reset["enforcement"] == "enforce"
    assert reset["inject"]["min_interval_s"] == 60
    assert changed_keys(updated, reset) == ["enforcement", "inject.min_interval_s"]


def test_empty_or_non_object_patch_is_rejected() -> None:
    with pytest.raises(SettingsError):
        apply_settings_patch(default_settings(), {})
    with pytest.raises(SettingsError):
        apply_settings_patch(default_settings(), ["enforcement"])  # type: ignore[arg-type]


def test_validate_reports_missing_and_unknown_keys() -> None:
    doc = default_settings()
    del doc["lease_ttl_s"]
    del doc["inject"]["compact_after"]
    doc["extra"] = True
    with pytest.raises(SettingsError) as exc:
        validate_settings(doc)
    assert exc.value.errors == {
        "lease_ttl_s": "missing",
        "inject.compact_after": "missing",
        "extra": "unknown setting",
    }


def test_load_settings_fills_keys_added_later_and_rejects_tampered_documents() -> None:
    old = {k: v for k, v in SPEC_DEFAULTS.items() if k not in ("readonly_fence_for_advisory", "notify")}
    old["enforcement"] = "observe"
    old["inject"] = {"turn_max_chars": 400}
    loaded = load_settings(dumps_settings(old))
    assert loaded["enforcement"] == "observe"
    assert loaded["readonly_fence_for_advisory"] is True
    assert loaded["inject"]["turn_max_chars"] == 400 and loaded["inject"]["compact_after"] == 40
    assert load_settings(None) == SPEC_DEFAULTS
    assert load_settings("") == SPEC_DEFAULTS
    with pytest.raises(SettingsError):
        load_settings('{"require_report_for_done": false}')
    with pytest.raises(SettingsError):
        load_settings("not json")
    with pytest.raises(SettingsError):
        load_settings("[1, 2]")


def test_settings_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "MAX_SETTINGS_BYTES", 200)
    with pytest.raises(SettingsError):
        validate_settings(default_settings())


def test_changed_keys_and_canonical_dump() -> None:
    a = default_settings()
    b = apply_settings_patch(a, {"strict_reports": False, "checkpoint": {"calls": 80}})
    assert changed_keys(a, b) == ["checkpoint.calls", "strict_reports"]
    assert changed_keys(a, a) == []
    assert dumps_settings(b) == dumps_settings(json.loads(dumps_settings(b)))
    assert json.loads(dumps_settings(b)) == b
