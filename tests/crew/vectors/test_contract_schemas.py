"""WP-0a contracts: schema language, closed event set, envelope, hash chain, snapshots, routes, MCP, scripts."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from remembra.crew import schemas as S
from tests.crew.vectors.fixtures import base_snapshot, event, session
from tests.crew.vectors.loader import load

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"

# ---------------------------------------------------------------------------
# Stdlib only (the gate runs `python -I` and vendors these modules)
# ---------------------------------------------------------------------------


def test_schemas_and_reducer_import_only_the_standard_library() -> None:
    probe = f"""
import sys, types, importlib.abc
allowed = set(sys.stdlib_module_names) | {{"remembra"}}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] not in allowed:
            raise ImportError("non-stdlib import: " + name)
        return None
sys.meta_path.insert(0, Block())
for pkg, rel in (("remembra", "remembra"), ("remembra.crew", "remembra/crew")):
    mod = types.ModuleType(pkg)
    mod.__path__ = [{str(SRC)!r} + "/" + rel]
    sys.modules[pkg] = mod
import remembra.crew.schemas as s, remembra.crew.reducer as r
assert s.guard_decide({{"no_zone_match": True}}).rule == 19
assert r.reduce({{"crew": {{"id": "crw_x", "mode": "solo"}}, "as_of_seq": 0}}, [])["last_seq"] == 0
print("ok")
"""
    out = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Schema language
# ---------------------------------------------------------------------------

SHAPE = S.Shape(
    "T",
    {
        "a": S._s(5, enum=("x", "yy")),
        "b": S._i(1, 3),
        "c": S._opt(S._s(10)),
        "d": S._l(S._b(), 2),
        "e": S._o(S.Shape("Inner", {"z": S._n()}), max_bytes=20),
        "f": S._any(10, req=False),
    },
)


def test_validate_accepts_a_valid_object() -> None:
    assert S.validate({"a": "x", "b": 2, "d": [True], "e": {"z": 1.5}}, SHAPE) == []
    assert S.validate({"a": "yy", "b": 3, "c": None, "d": [], "e": {"z": 0}, "f": [1]}, SHAPE) == []


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ({"b": 2, "d": [], "e": {"z": 1}}, "$.a: required"),
        ({"a": "q", "b": 2, "d": [], "e": {"z": 1}}, "not in"),
        ({"a": "x", "b": 4, "d": [], "e": {"z": 1}}, "above maximum"),
        ({"a": "x", "b": 0, "d": [], "e": {"z": 1}}, "below minimum"),
        ({"a": "x", "b": True, "d": [], "e": {"z": 1}}, "expected integer"),
        ({"a": "x", "b": 2, "d": [1], "e": {"z": 1}}, "expected boolean"),
        ({"a": "x", "b": 2, "d": [True, True, True], "e": {"z": 1}}, "more than 2 items"),
        ({"a": "x", "b": 2, "d": [], "e": {"z": "1"}}, "expected number"),
        ({"a": "x", "b": 2, "d": [], "e": {"z": 1}, "g": 1}, "$.g: unknown field"),
        ({"a": None, "b": 2, "d": [], "e": {"z": 1}}, "must not be null"),
        ({"a": "x", "b": 2, "d": [], "e": {"z": 123456789012345678}}, "larger than 20 bytes"),
        ({"a": "x", "b": 2, "d": [], "e": {"z": 1}, "f": "0123456789"}, "larger than 10 bytes"),
        ({"a": "x", "b": 2, "d": [], "e": {"z": 1}, "f": {1, 2}}, "not JSON-serializable"),
        ("nope", "expected object"),
    ],
)
def test_validate_rejects(value: object, fragment: str) -> None:
    errors = S.validate(value, SHAPE)
    assert any(fragment in e for e in errors), errors


def test_json_schema_export_is_closed_and_nullable() -> None:
    js = S.to_json_schema(SHAPE)
    assert js["additionalProperties"] is False
    assert set(js["required"]) == {"a", "b", "d", "e"}
    assert js["properties"]["c"]["anyOf"][1] == {"type": "null"}
    assert js["properties"]["a"]["enum"] == ["x", "yy"]


def test_path_rel_rejects_everything_that_would_leak_host_paths() -> None:
    ok = ["src/app/pos/cart.ts", ".remembra/zones.yml", "a b/c.ts", "src/app/café/x.ts", "."]
    bad = ["/Users/mani/x", "~/x", "../x", "src/../../x", "src\\x", "a/..", "x\x00y", ""]
    assert all(S.is_path_rel(p) for p in ok)
    assert not any(S.is_path_rel(p) for p in bad)


def test_ids_have_type_prefixes() -> None:
    assert S.is_id("session", "cs_a1")
    assert S.is_id("zone", "zn_pos")
    assert not S.is_id("session", "zn_pos")
    assert not S.is_id("claim", "clm_")
    assert not S.is_id("task", "tsk_" + "x" * 80)


# ---------------------------------------------------------------------------
# Closed event set and envelope (§4.1, §4.2)
# ---------------------------------------------------------------------------


def test_closed_event_set_matches_the_spec() -> None:
    assert len(S.EVENT_SPECS) == 100
    assert {t for t, s in S.EVENT_SPECS.items() if s.release == "L1"} == {
        "proposal.opened",
        "proposal.resolved",
        "vote.cast",
        "objection.raised",
    }
    assert set(S.CLIENT_EVENT_TYPES) == {
        "activity.burst",
        "activity.commit",
        "activity.push",
        "activity.deploy",
        "activity.test_verdict_changed",
        "guard.blocked",
        "guard.tamper_blocked",
        "gate.error",
        "gate.deadline",
        "githook.missing",
    }
    names = [s.payload.name for s in S.EVENT_SPECS.values()]
    assert len(names) == len(set(names))
    for spec in S.EVENT_SPECS.values():
        assert spec.type.split(".")[0] in {
            "crew",
            "host",
            "session",
            "activity",
            "zone",
            "claim",
            "baton",
            "guard",
            "gate",
            "githook",
            "collision",
            "task",
            "checkpoint",
            "report",
            "handoff",
            "message",
            "decision",
            "proposal",
            "vote",
            "objection",
            "inbox",
            "human",
            "budget",
        }


def test_every_l0_event_type_has_a_valid_sample() -> None:
    data = load("events/samples.json")
    types = [e["type"] for e in data["valid"]]
    assert sorted(types) == sorted(S.L0_EVENT_TYPES)
    for ev in data["valid"]:
        assert S.validate_envelope(ev) == [], ev["type"]


@pytest.mark.parametrize("case", load("events/samples.json")["invalid_envelopes"], ids=lambda c: c["name"])
def test_invalid_envelopes_are_rejected(case: dict) -> None:
    errors = S.validate_envelope(case["value"])
    assert any(case["error_contains"] in e for e in errors), errors


@pytest.mark.parametrize("case", load("events/samples.json")["invalid_client_events"], ids=lambda c: c["name"])
def test_invalid_client_events_are_rejected(case: dict) -> None:
    errors = S.validate_client_event(case["value"])
    assert any(case["error_contains"] in e for e in errors), errors


def test_valid_client_events_pass_and_l1_needs_opt_in() -> None:
    for item in load("events/samples.json")["valid_client_events"]:
        assert S.validate_client_event(item) == []
    payload = {"proposal_id": "prp_1", "choice": "a", "verified": True}
    assert S.validate_event_payload("vote.cast", payload) != []
    assert S.validate_event_payload("vote.cast", payload, allow_l1=True) == []


def test_moment_rules() -> None:
    col = {"collision": {"severity": "high"}}
    assert S.is_moment("collision.detected", col, "system")
    assert not S.is_moment("collision.detected", {"collision": {"severity": "medium"}}, "system")
    assert S.is_moment("activity.push", {"default_branch": True}, "session")
    assert not S.is_moment("activity.push", {"default_branch": False}, "session")
    assert S.is_moment("claim.adopted", {"cross_checkout": True}, "session")
    assert not S.is_moment("claim.adopted", {"cross_checkout": False}, "session")
    assert S.is_moment("zone.synced", {"policy_changed": True}, "system")
    assert not S.is_moment("zone.synced", {"policy_changed": False}, "system")
    assert S.is_moment("crew.mode_changed", {"to": "multi"}, "system")
    assert not S.is_moment("crew.mode_changed", {"to": "solo"}, "system")
    assert S.is_moment("task.assigned", {}, "human")  # every human-only action
    assert not S.is_moment("task.assigned", {}, "session")
    assert not S.is_moment("guard.blocked", {}, "human")  # client types are never "human actions"
    for t in ("task.done", "baton.passed", "session.lost", "guard.bypass_used", "gate.tampered", "zone.change_pending"):
        assert S.is_moment(t, {}, "system")
    for sev_kind, sev in S.COLLISION_SEVERITY.items():
        assert S.is_moment("collision.detected", {"collision": {"severity": sev}}, "system") == (
            sev_kind in ("same_worktree_file", "exclusive_breach", "foreign_checkout_write", "stale_epoch_write")
        )


def test_idempotency_prefixes_keep_client_and_server_keys_apart() -> None:
    assert S.client_idem_key("abc12345") == "c:abc12345"
    assert S.is_client_idem_key("c:abc12345")
    assert S.server_idem_key("join:crw_1:u:a:s") == "join:crw_1:u:a:s"
    with pytest.raises(ValueError):
        S.server_idem_key("c:forged")


# ---------------------------------------------------------------------------
# Hash chain (§4.1)
# ---------------------------------------------------------------------------


def _chain(n: int) -> list[dict]:
    prev = S.GENESIS_HASH
    events = []
    for ev in load("events/samples.json")["valid"][:n]:
        ev = copy.deepcopy(ev)
        ev["prev_hash"] = prev
        ev["hash"] = S.event_hash(prev, ev)
        prev = ev["hash"]
        events.append(ev)
    return events


def test_hash_chain_verifies_and_detects_tampering() -> None:
    events = _chain(20)
    assert S.verify_chain(events) == []
    tampered = copy.deepcopy(events)
    tampered[7]["summary"] = "rewritten"
    errors = S.verify_chain(tampered)
    assert any("seq 8: hash mismatch" in e for e in errors)
    dropped = events[:5] + events[6:]
    assert any("seq gap" in e for e in S.verify_chain(dropped))


def test_hash_ignores_hash_fields_and_key_order() -> None:
    ev = _chain(1)[0]
    reordered = dict(reversed(list(ev.items())))
    assert S.event_hash(S.GENESIS_HASH, reordered) == ev["hash"]
    assert S.canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()
    with pytest.raises(ValueError):
        S.canonical_json({"x": float("nan")})


# ---------------------------------------------------------------------------
# Snapshots, frames, tree (§4.4, §11)
# ---------------------------------------------------------------------------


def test_snapshots_validate_and_hmac_detects_rewrites() -> None:
    assert S.validate(base_snapshot(), S.SNAPSHOT) == []
    data = load("guard/concrete.json")
    snap = data["snapshot"]
    assert S.validate(snap, S.LOCAL_SNAPSHOT) == []
    key = data["hmac_key"].encode()
    assert S.verify_snapshot_hmac(key, snap)
    snap["claims"] = []
    assert not S.verify_snapshot_hmac(key, snap)
    assert not S.verify_snapshot_hmac(b"other-key", data["snapshot"])


def test_snapshot_never_carries_session_tokens() -> None:
    snap = base_snapshot(sessions=[{**session("cs_a", "cc-1"), "token_hash": "x"}])
    assert any("unknown field" in e for e in S.validate(snap, S.SNAPSHOT))


def test_ws_frames() -> None:
    ev = event(1, "task.created", {"task": {}})
    assert S.validate_ws_frame({"type": "crew.event", "crew_id": ev["crew_id"], "data": ev}) != []
    good = load("events/samples.json")["valid"][0]
    assert S.validate_ws_frame({"type": "crew.event", "crew_id": good["crew_id"], "data": good}) == []
    assert S.validate_ws_frame({"type": "resync_required", "crew_id": good["crew_id"], "reason": "overflow", "last_seq": 9}) == []
    assert S.validate_ws_frame({"type": "bogus"}) != []
    summary = {
        "type": "crew.summary",
        "crews": [{"crew_id": good["crew_id"], "project_id": "p", "mode": "multi", "live": 2, "moments": 1, "needs_you": 0}],
    }
    assert S.validate_ws_frame(summary) == []
    summary["crews"][0]["events"] = []  # counts only
    assert S.validate_ws_frame(summary) != []
    lane = {
        "session_id": "cs_a",
        "state": "active",
        "stuck": False,
        "last_action": {"tool": "Bash", "path_rel": None, "verb": "npm", "age_s": 2},
        "calls_since_checkpoint": 3,
        "next_checkpoint_due_at": None,
        "limit": None,
    }
    assert S.validate_ws_frame({"type": "presence", "crew_id": good["crew_id"], "lanes": [lane]}) == []
    leaky = {**lane, "last_action": {**lane["last_action"], "command": "curl -H 'Authorization: x'"}}
    assert S.validate_ws_frame({"type": "presence", "crew_id": good["crew_id"], "lanes": [leaky]}) != []
    assert S.validate({"type": "subscribe", "channel": "crew", "crew_id": "*", "topics": ["crew.summary"]}, S.WS_SUBSCRIBE) == []
    assert S.validate({"type": "subscribe", "channel": "crew", "crew_id": "x", "topics": ["crew"]}, S.WS_SUBSCRIBE) != []


def test_tree_snapshot_limits() -> None:
    ok = {
        "name": "src",
        "files": 3,
        "children": [
            {
                "name": "app",
                "files": 1,
                "children": [{"name": "pos", "files": 9, "children": [{"name": "ui", "files": 2, "children": []}]}],
            }
        ],
    }
    assert S.validate_tree(ok) == []
    too_deep = copy.deepcopy(ok)
    too_deep["children"][0]["children"][0]["children"][0]["children"] = [{"name": "x", "files": 0, "children": []}]
    assert any("deeper than 3" in e for e in S.validate_tree(too_deep))
    wide = {"name": "r", "files": 0, "children": [{"name": f"d{i}", "files": 0, "children": []} for i in range(400)]}
    assert any("more than 400 nodes" in e for e in S.validate_tree(wide))
    assert S.validate_tree({"name": "a/b", "files": -1, "children": []}) != []
    assert S.validate_tree({"name": "a", "files": 1, "content": "secret"}) != []


# ---------------------------------------------------------------------------
# REST route table and OpenAPI (§6)
# ---------------------------------------------------------------------------


def test_route_table_access_rules() -> None:
    op_ids = [r.operation_id for r in S.ROUTES]
    assert len(op_ids) == len(set(op_ids))
    seen = set()
    for r in S.ROUTES:
        assert (r.method, r.path) not in seen, r.path
        seen.add((r.method, r.path))
        assert r.access in S.ACCESS_KINDS
        if r.access == "existing":
            continue
        assert r.perm in S.PERMISSIONS, r.path
        # (H) routes need a human-only permission and vice versa (D27).
        assert r.human == (r.perm in S.HUMAN_ONLY_PERMISSIONS), r.path
        if r.step_up:
            assert r.human, r.path
        if "{crew_id}" in r.path:
            assert r.access == "crew", r.path
        if r.access == "entity":
            assert r.entity in S.ID_PREFIXES, r.path
            first_param = r.path.split("{")[1].split("}")[0]
            assert (
                first_param == f"{r.entity}_id"
                or (r.entity == "inbox_item" and first_param == "item_id")
                or (r.entity == "zone_change" and first_param == "change_id")
            ), r.path
        if r.request:
            assert r.request in S.REQUEST_SHAPES
    human_paths = {r.path.split("/")[-1] for r in S.ROUTES if r.human}
    for must in (
        "override",
        "freeze",
        "unfreeze",
        "release-all",
        "bypass-codes",
        "waive",
        "assign",
        "review",
        "confirm",
        "redact",
        "pin",
        "approve",
    ):
        assert must in human_paths, must
    step_up = {(r.method, r.path) for r in S.ROUTES if r.step_up}
    assert ("POST", "/claims/{claim_id}/override") in step_up
    assert ("PATCH", "/crews/{crew_id}") in step_up
    assert ("POST", "/zones/{zone_id}/unfreeze") in step_up
    assert ("POST", "/tasks/{task_id}/waive") in step_up
    assert ("POST", "/crews/{crew_id}/bypass-codes") in step_up
    assert ("POST", "/zone-changes/{change_id}/approve") in step_up
    l1 = {r.path for r in S.ROUTES if r.release == "L1"}
    assert "/crews/{crew_id}/tasks/from-handoff" in l1
    assert "/proposals/{proposal_id}/votes" in l1
    # Static /crews/* paths must be registered before /crews/{crew_id} (FastAPI matches in order).
    order = [r.path for r in S.ROUTES]
    assert order.index("/crews/inbox/overview") < order.index("/crews/{crew_id}")


def test_request_shapes_enforce_caps_and_never_accept_tokens_in_join() -> None:
    join = {"agent_id": "claude-code", "session_id": "s1", "adapter": "claude-code", "client_kind": "hook", "source": "startup"}
    assert S.validate(join, S.REQUEST_SHAPES["Join"]) == []
    assert S.validate({**join, "session_token": "x"}, S.REQUEST_SHAPES["Join"]) != []
    claim = {"zone_id": "zn_pos", "mode": "exclusive", "wait": True, "wait_s": 301, "source": "mcp"}
    assert any("above maximum" in e for e in S.validate(claim, S.REQUEST_SHAPES["Claim"]))
    msg = {"kind": "chat", "body": "x" * (S.MAX_MESSAGE_BYTES + 1), "client_msg_id": "m1"}
    assert S.validate(msg, S.REQUEST_SHAPES["Message"]) != []
    assert S.validate({**msg, "kind": "proposal", "body": "x"}, S.REQUEST_SHAPES["Message"]) != []  # L1 kind
    assert S.validate({"code": "RCB-7K3QW-9ZX2M", "session_id": "cs_a"}, S.REQUEST_SHAPES["BypassRedeem"]) == []
    assert S.validate({"code": "RCB-7K3QW-9ZX2O", "session_id": "cs_a"}, S.REQUEST_SHAPES["BypassRedeem"]) != []
    assert S.validate({"session_id": "cs_a", "scope": "push", "minutes": 16}, S.REQUEST_SHAPES["BypassIssue"]) != []


def test_openapi_file_is_generated_from_the_route_table() -> None:
    on_disk = (ROOT / "docs" / "crew" / "openapi.json").read_text(encoding="utf-8")
    assert on_disk == S.render_openapi(), (
        'regenerate: PYTHONPATH=src python -c "from remembra.crew.schemas import render_openapi; '
        "open('docs/crew/openapi.json', 'w').write(render_openapi())\""
    )
    spec = json.loads(on_disk)
    assert spec["openapi"] == "3.1.0"
    ops = [op for path in spec["paths"].values() for op in path.values()]
    assert len(ops) == len(S.ROUTES)

    def refs(node: object) -> list[str]:
        if isinstance(node, dict):
            own = [node["$ref"]] if "$ref" in node else []
            return own + [r for v in node.values() for r in refs(v)]
        if isinstance(node, list):
            return [r for v in node for r in refs(v)]
        return []

    for ref in refs(spec):
        _, section, name = ref.lstrip("#/").split("/")
        assert name in spec["components"][section], ref
    human = [op for op in ops if op["x-human-only"]]
    assert all(op["x-permission"] in S.HUMAN_ONLY_PERMISSIONS for op in human)


# ---------------------------------------------------------------------------
# MCP tools (§7)
# ---------------------------------------------------------------------------


def test_mcp_tool_signatures() -> None:
    names = [t.name for t in S.MCP_TOOLS]
    assert names == ["crew_status", "crew_claim", "crew_guard", "crew_task", "crew_say", "crew_checkpoint", "crew_report"]
    claim = next(t for t in S.MCP_TOOLS if t.name == "crew_claim")
    assert next(p for p in claim.params if p.name == "wait_s").max_value == 300
    say = next(t for t in S.MCP_TOOLS if t.name == "crew_say")
    assert next(p for p in say.params if p.name == "wait_s").max_value == 120
    assert S.validate_mcp_call("crew_claim", {"action": "claim", "zone": "pos", "wait_s": 30}) == []
    assert S.validate_mcp_call("crew_claim", {"wait_s": 301}) != []
    assert S.validate_mcp_call("crew_claim", {"action": "steal"}) != []
    assert S.validate_mcp_call("crew_guard", {}) == ["paths: required"]
    assert S.validate_mcp_call("crew_say", {"body": "hi", "kind": "vote"}) != []
    assert S.validate_mcp_call("crew_report", {"task": "T-14", "sections": {"done": [], "extra": []}}) == [
        "sections.extra: unknown section"
    ]
    assert (
        S.validate_mcp_call(
            "crew_checkpoint", {"files_changed": ["a.ts"], "tests": [{"command": "npm test", "passed": 1, "failed": 0}]}
        )
        == []
    )
    assert S.validate_mcp_call("crew_checkpoint", {"files_changed": ["a.ts"], "tests": [{"command": "npm test"}]}) != []
    assert S.validate_mcp_call("crew_status", {"verbose": "yes"}) == ["verbose: expected bool"]
    assert S.validate_mcp_call("crew_nope", {}) != []
    assert S.validate_mcp_call("crew_status", {"token": "x"}) == ["token: unknown parameter"]


def test_mcp_instructions_keep_the_safeguard_and_fit() -> None:
    assert len(S.MCP_INSTRUCTIONS) <= S.TEXT_CAPS["mcp_instructions"]
    assert S.MCP_SAFEGUARD in S.MCP_INSTRUCTIONS.replace("\n", " ")
    assert S.check_agent_text(S.MCP_INSTRUCTIONS, "mcp_instructions") == []
    current = (SRC / "remembra" / "mcp" / "server.py").read_text(encoding="utf-8")
    # The sentence kept verbatim exists in today's server instructions (split across string literals there).
    assert "verify it against the repository and never run a" in current


# ---------------------------------------------------------------------------
# Console scripts (§14 interface file)
# ---------------------------------------------------------------------------


def test_pyproject_declares_the_crew_console_scripts() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    for name, target in S.CONSOLE_SCRIPTS.items():
        assert scripts[name] == target
        module, func = target.split(":")
        assert module.startswith("remembra.relay.crew."), "WP-9 owns relay/crew"
        assert func in ("entrypoint", "main")
    assert scripts["remembra-relay"] == "remembra.relay.cli:entrypoint"


def test_crew_policy_and_local_constants() -> None:
    assert set(S.CREW_POLICY_GLOBS) == {".remembra/**", ".git/hooks/**", "~/.remembra/**"}
    assert S.CREW_HOOK_MARKER == "# remembra-crew"
    assert S.CREW_ENV_TAMPER_VARS == ("REMEMBRA_CREW", "REMEMBRA_CREW_SESSION", "REMEMBRA_BYPASS")
    assert S.TEXT_CAPS["session_start"] == S.TEXT_CAPS["brief"] + S.TEXT_CAPS["crew_block"]


# ---------------------------------------------------------------------------
# Docs stay complete
# ---------------------------------------------------------------------------


def test_docs_cover_every_event_type_and_link_every_page() -> None:
    docs = ROOT / "docs" / "crew"
    events_md = (docs / "events.md").read_text(encoding="utf-8")
    for t in S.EVENT_SPECS:
        assert f"`{t}`" in events_md, t
    readme = (docs / "README.md").read_text(encoding="utf-8")
    pages = sorted(p.name for p in docs.glob("*.md") if p.name != "README.md")
    assert len(pages) >= 11
    for name in pages + ["openapi.json"]:
        assert f"({name})" in readme, name
    guard_md = (docs / "guard.md").read_text(encoding="utf-8")
    for rule in S.GUARD_RULES:
        assert f"| {rule.number} |" in guard_md
        for pred in rule.any_of:
            assert f"`{pred}`" in guard_md, pred
    for mod in S.GUARD_MODIFIERS:
        assert f"`{mod}`" in guard_md, mod
