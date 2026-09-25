"""Load crew contract vectors and run them against an implementation.

Downstream work packages call these runners from their own tests, e.g. WP-3::

    from tests.crew.vectors.loader import run_bash_corpus
    assert run_bash_corpus(gatecore.parse_bash) == []

Every runner returns a list of human-readable failures (empty = conformant).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

VECTOR_DIR = Path(__file__).resolve().parent
_MISSING = object()


@cache
def _load_text(rel: str) -> str:
    return (VECTOR_DIR / rel).read_text(encoding="utf-8")


def load(rel: str) -> Any:
    """A fresh copy of one vector file (callers may mutate it)."""
    return json.loads(_load_text(rel))


def reducer_vectors() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((VECTOR_DIR / "reducer").glob("*.json"))]


def get_path(state: Any, path: list[str | int]) -> Any:
    cur = state
    for seg in path:
        if isinstance(seg, int):
            if not isinstance(cur, list) or seg >= len(cur):
                return _MISSING
            cur = cur[seg]
        else:
            if not isinstance(cur, dict) or seg not in cur:
                return _MISSING
            cur = cur[seg]
    return cur


def check_assertions(state: Any, expect: list[Mapping[str, Any]]) -> list[str]:
    failures: list[str] = []
    for a in expect:
        path = a["path"]
        value = get_path(state, path)
        where = ".".join(str(p) for p in path) or "<root>"
        if a.get("absent"):
            if value is not _MISSING:
                failures.append(f"{where}: expected absent, got {value!r}")
        elif "length" in a:
            if value is _MISSING or not hasattr(value, "__len__") or len(value) != a["length"]:
                got = "missing" if value is _MISSING else (len(value) if hasattr(value, "__len__") else value)
                failures.append(f"{where}: expected length {a['length']}, got {got}")
        elif value is _MISSING:
            failures.append(f"{where}: missing (expected {a['equals']!r})")
        elif value != a["equals"] or type(value) is not type(a["equals"]):
            failures.append(f"{where}: expected {a['equals']!r}, got {value!r}")
    return failures


def run_reducer_vectors(reduce_fn: Callable[[Any, list[Any]], Any]) -> dict[str, list[str]]:
    """``reduce_fn(snapshot, frames) -> state`` (JSON-shaped). Returns {vector name: failures} for failing vectors."""
    out: dict[str, list[str]] = {}
    for v in reducer_vectors():
        state = json.loads(json.dumps(reduce_fn(v["snapshot"], v["frames"])))  # JSON round trip, as TS would see it
        failures = check_assertions(state, v["expect"])
        if failures:
            out[v["name"]] = failures
    return out


def run_guard_table(decide_fn: Callable[..., Mapping[str, Any]]) -> list[str]:
    """``decide_fn(facts, mode, interactive_override=, permission_mode=) -> {rule, decision, variant, effects}``."""
    failures: list[str] = []
    for case in load("guard/table.json")["cases"]:
        got = dict(
            decide_fn(
                case["facts"],
                case["mode"],
                interactive_override=case.get("interactive_override", False),
                permission_mode=case.get("permission_mode", "default"),
            )
        )
        exp = case["expect"]
        if {k: got.get(k) for k in exp} != exp:
            failures.append(f"{case['name']}: expected {exp}, got {got}")
    return failures


def run_guard_concrete(evaluate_fn: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]) -> list[str]:
    """``evaluate_fn(case, context) -> {rule, decision, variant[, facts]}``.

    ``context`` = {"snapshot", "home", "hmac_key"}. When the result carries
    ``facts``, they are compared too (only the keys the vector lists).
    """
    data = load("guard/concrete.json")
    context = {"snapshot": data["snapshot"], "home": data["home"], "hmac_key": data["hmac_key"]}
    failures: list[str] = []
    for case in data["cases"]:
        got = dict(evaluate_fn(case, context))
        exp = case["expect"]
        if {k: got.get(k, "") for k in exp} != exp:
            failures.append(f"{case['name']}: expected {exp}, got { {k: got.get(k) for k in exp} }")
        if "facts" in got:
            want = {k: v for k, v in case["facts"].items()}
            have = {k: got["facts"].get(k) for k in want}
            if have != want:
                failures.append(f"{case['name']}: facts expected {want}, got {have}")
    return failures


def bash_expected(entry: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    return {**defaults, **entry["expect"]}


def run_bash_corpus(parse_fn: Callable[[str], Mapping[str, Any]]) -> list[str]:
    """``parse_fn(command) -> {read_only, writes, tree_writer, tree_scope, tamper, git_tree_op, opaque}``."""
    data = load("bash/corpus.json")
    failures: list[str] = []
    for entry in data["entries"]:
        exp = bash_expected(entry, data["defaults"])
        raw = dict(parse_fn(entry["cmd"]))
        got = {k: raw.get(k) for k in exp}
        for key in ("writes", "tree_scope", "tamper"):
            if isinstance(got.get(key), list | tuple | set):
                got[key] = sorted(set(got[key]))
        if got != exp:
            diff = {k: (exp[k], got[k]) for k in exp if exp[k] != got[k]}
            failures.append(f"{entry['id']} {entry['cmd']!r}: (expected, got) {diff}")
    return failures


def run_mcp_tool_map(classify_fn: Callable[[str, Mapping[str, Any], list[Any]], Mapping[str, Any]]) -> list[str]:
    """``classify_fn(tool_name, tool_input, zone_rules) -> {kind, paths, services, zone_slug, github_repo}``."""
    failures: list[str] = []
    for case in load("mcp/tool_map.json")["cases"]:
        rules = [(slug, rule) for slug, rule in case["zone_rules"]]
        got = dict(classify_fn(case["tool"], case["input"], rules))
        got = {k: (list(v) if isinstance(v, tuple) else v) for k, v in got.items()}
        if {k: got.get(k) for k in case["expect"]} != case["expect"]:
            failures.append(f"{case['tool']}: expected {case['expect']}, got {got}")
    return failures


def run_command_grammar(
    validate_fn: Callable[[Any], list[str]],
    validate_list_fn: Callable[[Any], list[str]],
    match_fn: Callable[[str, list[str]], bool],
) -> list[str]:
    data = load("grammar/command_patterns.json")
    failures: list[str] = []
    for p in data["valid"]:
        if validate_fn(p):
            failures.append(f"valid pattern rejected: {p!r}: {validate_fn(p)}")
    for case in data["invalid"]:
        errors = validate_fn(case["pattern"])
        if not any(case["error_contains"] in e for e in errors):
            failures.append(
                f"invalid pattern {case['pattern'][:40]!r}: expected error containing {case['error_contains']!r}, got {errors}"
            )
    for case in data["invalid_lists"]:
        errors = validate_list_fn(case["patterns"])
        if not any(case["error_contains"] in e for e in errors):
            failures.append(f"invalid list: expected {case['error_contains']!r}, got {errors}")
    for case in data["match"]:
        if bool(match_fn(case["pattern"], case["argv"])) != case["match"]:
            failures.append(f"match {case['pattern']!r} {case['argv']}: expected {case['match']}")
    return failures


def run_hook_stdout(
    validate_fn: Callable[[str, str], list[str]], builders: Mapping[str, Callable[..., str]] | None = None
) -> list[str]:
    """``validate_fn(hook, stdout) -> errors``; ``builders`` maps builder names to callables (optional)."""
    data = load("hooks/stdout.json")
    failures: list[str] = []
    for case in data["valid"]:
        errors = validate_fn(case["hook"], case["stdout"])
        if errors:
            failures.append(f"valid {case['hook']} rejected: {errors} :: {case['stdout'][:80]!r}")
    for case in data["invalid"]:
        errors = validate_fn(case["hook"], case["stdout"])
        if not any(case["error_contains"] in e for e in errors):
            failures.append(f"invalid {case['hook']} {case['stdout'][:60]!r}: expected {case['error_contains']!r}, got {errors}")
    if builders is not None:
        for case in data["builders"]:
            fn = builders[case["builder"]]
            out = fn() if case["builder"] == "hook_allow" else fn(case["arg"])
            if out != case["stdout"]:
                failures.append(f"builder {case['builder']}: expected {case['stdout'][:80]!r}, got {out[:80]!r}")
        for case in data["builder_rejects"]:
            try:
                builders[case["builder"]](case["arg"])
            except ValueError as exc:
                if case["error_contains"] not in str(exc):
                    failures.append(f"builder {case['builder']} rejected with {exc}, expected {case['error_contains']!r}")
            else:
                failures.append(f"builder {case['builder']} accepted {case['arg'][:60]!r}")
    return failures


def run_agent_text(check_fn: Callable[[str, str], list[str]], clip_fn: Callable[[str], str] | None = None) -> list[str]:
    data = load("hooks/agent_text.json")
    failures: list[str] = []
    for case in data["valid"]:
        errors = check_fn(case["text"], case["channel"])
        if errors:
            failures.append(f"valid {case['channel']} rejected: {errors}")
    for case in data["invalid"]:
        errors = check_fn(case["text"], case["channel"])
        if not any(case["error_contains"] in e for e in errors):
            failures.append(f"invalid {case['channel']} {case['text'][:60]!r}: expected {case['error_contains']!r}, got {errors}")
    if clip_fn is not None:
        for case in data["clip"]:
            if clip_fn(case["input"]) != case["output"]:
                failures.append(
                    f"clip {case['input'][:30]!r}: expected {case['output'][:30]!r}, got {clip_fn(case['input'])[:30]!r}"
                )
    return failures


def run_redaction_corpus(outbound_fn: Callable[..., Any]) -> list[str]:
    """``outbound_fn(payload_type, payload, repo_root=, home=) -> payload`` (the single choke point, §11)."""
    data = load("redaction/corpus.json")
    ctx = data["context"]
    failures: list[str] = []
    for case in data["cases"]:
        out = outbound_fn(case["payload_type"], case["payload"], repo_root=ctx["repo_root"], home=ctx["home"])
        text = json.dumps(out, ensure_ascii=False)
        for secret in case["must_not_contain"]:
            if secret in text:
                failures.append(f"{case['id']} ({case['payload_type']}): leaked {secret[:12]}…")
        for keep in case["must_contain"]:
            if keep not in text:
                failures.append(f"{case['id']} ({case['payload_type']}): lost {keep!r}")
    return failures
