"""``crews.settings``: defaults, validation and patching (spec §3.2).

Settings are versioned JSON stored on the ``crews`` row. They are patched with
``If-Match`` (``settings_version``) and only by a human principal (D27; the
route layer enforces that, see ``crew/access.py``). This module is pure: it
knows nothing about the database or who is asking. It guarantees that every
stored settings document is complete, typed and inside safe bounds, so the
guard, reaper and gate can read any key without defensive code.

Rules that come straight from the spec:

* ``require_report_for_done`` cannot be set to false (§3.2).
* ``enforcement`` is one of ``off | observe | enforce`` (§2).
* ``undeclared_policy`` is one of ``footprint | file_claim`` (D8).
* ``inject`` budgets never exceed the agent-facing text caps (§10.4).
* ``lease_ttl_s`` leaves room for the 60 s fence before expiry (D31).
* ``live_check_domains`` are public DNS host names, never IP literals or
  internal names, because the server fetches them (§5.6 SSRF rules).
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Final

from remembra.crew import schemas

DEFAULT_SETTINGS: Final[Mapping[str, Any]] = {
    "enforcement": "enforce",
    "lease_ttl_s": 600,
    "idle_park_s": 3600,
    "reserve_ttl_s": 86400,
    "task_baton_expiry": "never",
    "host_lost_after_s": 1800,
    "auto_claim": True,
    "auto_claim_leaf_only": True,
    "max_exclusive_claims_per_session": 3,
    "max_exclusive_claims_per_agent": 6,
    "undeclared_policy": "footprint",
    "no_zone_bootstrap": "suggested_enforce",
    "auto_adopt": "same_checkout",
    "adopt_on_first_write": False,
    "baton_push": "auto",
    "require_report_for_done": True,
    "strict_reports": True,
    "deploy_gate": {"require_pushed": True, "require_live_check": False},
    "live_check_domains": [],
    "wip_per_session": 1,
    "inject": {"turn_max_chars": 600, "pretool_max_chars": 300, "min_interval_s": 60, "compact_after": 40},
    "checkpoint": {"interval_s": 600, "calls": 40},
    "fail_closed_zones": [],
    "interactive_override": False,
    "require_verified_agents_for_claims": False,
    "readonly_fence_for_advisory": True,
    "notify": {"realtime": ["email"]},
}

MAX_SETTINGS_BYTES: Final = 16 * 1024
MAX_LIVE_CHECK_DOMAINS: Final = 20
MAX_FAIL_CLOSED_ZONES: Final = 50
NOTIFY_CHANNELS: Final = ("email", "webhook")

_HOSTNAME_RE: Final = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
_INTERNAL_SUFFIXES: Final = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".test", ".invalid")
_SLUG_RE: Final = re.compile(schemas.SLUG_PATTERN)


class SettingsError(ValueError):
    """A settings document or patch is invalid. ``errors`` maps a dotted key path to a reason."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors: dict[str, str] = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in sorted(self.errors.items())))


# ---------------------------------------------------------------------------
# Field validators. Each returns an error string, or None when the value is valid.
# ---------------------------------------------------------------------------

Check = Callable[[Any], str | None]


def _bool() -> Check:
    def check(v: Any) -> str | None:
        return None if isinstance(v, bool) else "must be true or false"

    return check


def _int(lo: int, hi: int) -> Check:
    def check(v: Any) -> str | None:
        if isinstance(v, bool) or not isinstance(v, int):
            return "must be an integer"
        if not lo <= v <= hi:
            return f"must be between {lo} and {hi}"
        return None

    return check


def _enum(*values: str) -> Check:
    def check(v: Any) -> str | None:
        return None if isinstance(v, str) and v in values else f"must be one of {', '.join(values)}"

    return check


def _const(value: Any, reason: str) -> Check:
    def check(v: Any) -> str | None:
        return None if (type(v) is type(value) and v == value) else reason

    return check


def _task_baton_expiry(v: Any) -> str | None:
    # D5 / owner Q1: task-linked batons never silently expire by default. The owner
    # may choose a hard expiry, but never shorter than the 24 h first reminder.
    if v == "never":
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        return 'must be "never" or a number of seconds'
    if not 86400 <= v <= 30 * 86400:
        return "must be between 86400 and 2592000 seconds"
    return None


def _hostname_list(v: Any) -> str | None:
    if not isinstance(v, list):
        return "must be a list of host names"
    if len(v) > MAX_LIVE_CHECK_DOMAINS:
        return f"at most {MAX_LIVE_CHECK_DOMAINS} domains"
    for item in v:
        if not isinstance(item, str):
            return "must be a list of host names"
        host = item.strip().lower().rstrip(".")
        if host != item:
            return f"{item!r} must be lower-case with no surrounding spaces or trailing dot"
        if not _HOSTNAME_RE.fullmatch(host):
            return f"{item!r} is not a public DNS host name (no scheme, port, path or IP address)"
        if host.endswith(_INTERNAL_SUFFIXES) or host in ("localhost",):
            return f"{item!r} is an internal host name"
    if len(set(v)) != len(v):
        return "must not contain duplicates"
    return None


def _slug_list(v: Any) -> str | None:
    if not isinstance(v, list):
        return "must be a list of zone slugs"
    if len(v) > MAX_FAIL_CLOSED_ZONES:
        return f"at most {MAX_FAIL_CLOSED_ZONES} zones"
    for item in v:
        if not isinstance(item, str) or not _SLUG_RE.fullmatch(item):
            return f"{item!r} is not a zone slug"
    if len(set(v)) != len(v):
        return "must not contain duplicates"
    return None


def _notify_channels(v: Any) -> str | None:
    if not isinstance(v, list):
        return "must be a list of channels"
    for item in v:
        if item not in NOTIFY_CHANNELS:
            return f"{item!r} is not one of {', '.join(NOTIFY_CHANNELS)}"
    if len(set(v)) != len(v):
        return "must not contain duplicates"
    return None


# Top-level scalar and list keys.
_FIELDS: Final[Mapping[str, Check]] = {
    "enforcement": _enum(*schemas.ENFORCEMENT_LEVELS),
    # The holder is fenced at lease - 60 s (D31): leave at least a minute of useful lease.
    "lease_ttl_s": _int(120, 3600),
    "idle_park_s": _int(300, 86400),
    "reserve_ttl_s": _int(3600, 30 * 86400),
    "task_baton_expiry": _task_baton_expiry,
    "host_lost_after_s": _int(600, 86400),
    "auto_claim": _bool(),
    "auto_claim_leaf_only": _bool(),
    "max_exclusive_claims_per_session": _int(1, 10),
    "max_exclusive_claims_per_agent": _int(1, 30),
    "undeclared_policy": _enum(*schemas.UNDECLARED_POLICIES),
    "no_zone_bootstrap": _enum("suggested_enforce", "off"),
    "auto_adopt": _enum("same_checkout", "off"),
    "adopt_on_first_write": _bool(),
    "baton_push": _enum("auto", "local"),
    "require_report_for_done": _const(True, "cannot be turned off: every finished task needs a report"),
    "strict_reports": _bool(),
    "live_check_domains": _hostname_list,
    "wip_per_session": _int(1, 5),
    "fail_closed_zones": _slug_list,
    "interactive_override": _bool(),
    "require_verified_agents_for_claims": _bool(),
    "readonly_fence_for_advisory": _bool(),
}

# Nested objects: every sub-key is fixed.
_NESTED: Final[Mapping[str, Mapping[str, Check]]] = {
    "deploy_gate": {"require_pushed": _bool(), "require_live_check": _bool()},
    "inject": {
        "turn_max_chars": _int(100, schemas.TEXT_CAPS["turn"]),
        "pretool_max_chars": _int(50, schemas.TEXT_CAPS["pretool_context"]),
        "min_interval_s": _int(0, 3600),
        "compact_after": _int(1, 1000),
    },
    "checkpoint": {"interval_s": _int(60, 3600), "calls": _int(5, 500)},
    "notify": {"realtime": _notify_channels},
}

assert set(_FIELDS) | set(_NESTED) == set(DEFAULT_SETTINGS), "every default needs a validator"


def default_settings() -> dict[str, Any]:
    """A fresh, mutable copy of the defaults."""
    return copy.deepcopy(dict(DEFAULT_SETTINGS))


def _cross_field_errors(s: Mapping[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if s["max_exclusive_claims_per_agent"] < s["max_exclusive_claims_per_session"]:
        errors["max_exclusive_claims_per_agent"] = "must be at least max_exclusive_claims_per_session"
    if s["deploy_gate"]["require_live_check"] and not s["live_check_domains"]:
        errors["deploy_gate.require_live_check"] = "needs at least one entry in live_check_domains"
    return errors


def validate_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a COMPLETE settings document; return a normalised deep copy.

    Raises :class:`SettingsError` listing every problem (unknown keys, missing
    keys, wrong types, out-of-range values, cross-field rules).
    """
    if not isinstance(settings, Mapping):
        raise SettingsError({"": "settings must be a JSON object"})
    errors: dict[str, str] = {}
    for key in settings:
        if key not in DEFAULT_SETTINGS:
            errors[str(key)] = "unknown setting"
    for key, check in _FIELDS.items():
        if key not in settings:
            errors[key] = "missing"
            continue
        reason = check(settings[key])
        if reason:
            errors[key] = reason
    for key, sub_checks in _NESTED.items():
        value = settings.get(key)
        if key not in settings:
            errors[key] = "missing"
            continue
        if not isinstance(value, Mapping):
            errors[key] = "must be an object"
            continue
        for sub in value:
            if sub not in sub_checks:
                errors[f"{key}.{sub}"] = "unknown setting"
        for sub, check in sub_checks.items():
            if sub not in value:
                errors[f"{key}.{sub}"] = "missing"
                continue
            reason = check(value[sub])
            if reason:
                errors[f"{key}.{sub}"] = reason
    if errors:
        raise SettingsError(errors)
    normalised = copy.deepcopy(dict(settings))
    errors = _cross_field_errors(normalised)
    if errors:
        raise SettingsError(errors)
    if len(dumps_settings(normalised).encode()) > MAX_SETTINGS_BYTES:
        raise SettingsError({"": f"settings exceed {MAX_SETTINGS_BYTES} bytes"})
    return normalised


def apply_settings_patch(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into ``current`` and validate the result.

    Top-level keys replace; nested objects (``deploy_gate``, ``inject``,
    ``checkpoint``, ``notify``) merge one level deep, so ``{"inject":
    {"min_interval_s": 120}}`` keeps the other inject budgets. A ``null`` value
    resets that key to its default. Unknown keys are rejected, never dropped.
    """
    if not isinstance(patch, Mapping):
        raise SettingsError({"": "patch must be a JSON object"})
    if not patch:
        raise SettingsError({"": "patch is empty"})
    merged = copy.deepcopy(dict(current))
    errors: dict[str, str] = {}
    for key, value in patch.items():
        if key not in DEFAULT_SETTINGS:
            errors[str(key)] = "unknown setting"
            continue
        if value is None:
            merged[key] = copy.deepcopy(DEFAULT_SETTINGS[key])
        elif key in _NESTED and isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            inner = dict(merged[key])
            for sub, sub_value in value.items():
                if sub_value is None and sub in DEFAULT_SETTINGS[key]:
                    inner[sub] = copy.deepcopy(DEFAULT_SETTINGS[key][sub])
                else:
                    inner[sub] = copy.deepcopy(sub_value)
            merged[key] = inner
        else:
            merged[key] = copy.deepcopy(value)
    if errors:
        raise SettingsError(errors)
    return validate_settings(merged)


def load_settings(raw: str | Mapping[str, Any] | None) -> dict[str, Any]:
    """Read a stored settings document, filling keys added after it was written.

    Stored documents are always validated on write, so a document that fails
    validation here was written by something other than this module: it raises
    rather than silently weakening protection.
    """
    if raw is None or raw == "":
        stored: Mapping[str, Any] = {}
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SettingsError({"": f"stored settings are not valid JSON: {e.msg}"}) from e
        if not isinstance(parsed, dict):
            raise SettingsError({"": "stored settings must be a JSON object"})
        stored = parsed
    else:
        stored = raw
    full = default_settings()
    for key, value in stored.items():
        if key in _NESTED and isinstance(value, Mapping):
            full[key] = {**full[key], **value}
        else:
            full[key] = value
    return validate_settings(full)


def dumps_settings(settings: Mapping[str, Any]) -> str:
    """Canonical JSON for storage (sorted keys, compact)."""
    return json.dumps(settings, sort_keys=True, separators=(",", ":"))


def changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Dotted key paths whose value differs (for ``crew.settings_changed.changed_keys``)."""
    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            out.extend(f"{key}.{sub}" for sub in sorted(set(a) | set(b)) if a.get(sub) != b.get(sub))
        elif a != b:
            out.append(key)
    return out
