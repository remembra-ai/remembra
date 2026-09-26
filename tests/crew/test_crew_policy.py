"""WP-5 zone policy: zones.yml parsing/validation, loosening diff, interim policy, export, overlap, suggestions."""

from __future__ import annotations

import pytest

from remembra.crew import policy as P
from remembra.crew import schemas as S
from tests.crew.wp5_support import ZONES_YML


def test_parse_full_document_and_shorthand():
    pol = P.parse_zones_yaml(ZONES_YML)
    assert pol.slugs == ["app", "pos", "reports", "billing"]
    pos = pol.zone("pos")
    assert (
        pos is not None and pos.parent == "app" and pos.include == ("src/app/pos/**",) and pos.commands == ("supabase db push *",)
    )
    assert pol.commons == (("package.json", "plain"),)
    assert pol.ignore == ("docs/**",)
    short = P.parse_zones_yaml("zones:\n  pos: [src/app/pos/**]\n  x: src/x\n")
    assert short.zone("pos").include == ("src/app/pos/**",)  # type: ignore[union-attr]
    assert short.zone("x").include == ("src/x",)  # type: ignore[union-attr]
    listed = P.parse_zones_yaml("zones:\n  - slug: pos\n    include: [a/**]\n")
    assert listed.slugs == ["pos"]
    assert P.parse_zones_yaml("") == P.Policy()


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("zones:\n  pos: {include: [a/**], protectd: true}\n", "unknown key"),
        ("zones:\n  crew-policy: [a/**]\n", "built-in"),
        ("zones:\n  POS: [a/**]\n", "slug"),
        ("zones:\n  pos: {include: [/etc/passwd]}\n", "repo-relative"),
        ("zones:\n  pos: {include: [../x/**]}\n", ".."),
        ("zones:\n  pos: {include: [a/**], mode: locked}\n", "mode"),
        ("zones:\n  pos: {include: [a/**], commands: ['^rm.*']}\n", "commands"),
        ("zones:\n  pos: {include: [a/**], commands: ['vercel*']}\n", "whole token"),
        ("zones:\n  pos: {include: [a/**], parent: nope}\n", "unknown zone"),
        ("zones:\n  a: {include: [a/**], parent: b}\n  b: {include: [b/**], parent: a}\n", "cycle"),
        ("zones:\n  pos: {}\n", "at least one glob"),
        ("zones:\n  pos: {include: [a/**], services: ['vercel']}\n", "services"),
        ("zones:\n  pos: {include: [a/**], reserve_for: 'bad agent'}\n", "reserve_for"),
        ("zones:\n  pos: {include: [a/**], protected: 'yes'}\n", "true or false"),
        ("commons: {package.json: odd}\n", "kind"),
        ("version: 2\n", "version"),
        ("enforcement: loose\n", "enforcement"),
        ("bogus: 1\n", "unknown key"),
        ("zones: [1, 2]\n", "slug"),
        ("a: &x [1]\nb: *x\n", "anchors are not allowed"),
        ("zones: [\n", "not valid YAML"),
        ("- just a list\n", "mapping"),
    ],
)
def test_invalid_documents_are_refused_with_reasons(text, needle):
    with pytest.raises(P.PolicyError) as err:
        P.parse_zones_yaml(text)
    assert any(needle in e for e in err.value.errors), err.value.errors


def test_size_cap_and_glob_cap():
    with pytest.raises(P.PolicyError):
        P.parse_zones_yaml("#" * (S.MAX_ZONES_YML_BYTES + 1))
    globs = ", ".join(f"g{i}/**" for i in range(S.MAX_GLOBS_PER_ZONE + 1))
    with pytest.raises(P.PolicyError):
        P.parse_zones_yaml(f"zones:\n  pos: [{globs}]\n")


def test_default_commons_kinds():
    pol = P.parse_zones_yaml("commons: [yarn.lock, db/migrations/**, package.json]\n")
    assert dict(pol.commons) == {"yarn.lock": "serialize", "db/migrations/**": "append_only", "package.json": "plain"}
    eff = P.effective_commons(pol)
    assert eff[0] == {"glob": "yarn.lock", "kind": "serialize"}
    assert {"glob": "**/migrations/**", "kind": "append_only"} in eff
    assert {"glob": "**/package-lock.json", "kind": "serialize"} in eff


def test_export_round_trips():
    pol = P.parse_zones_yaml(ZONES_YML + "enforcement: observe\n")
    text = P.export_yaml(pol)
    assert P.parse_zones_yaml(text) == pol
    patch = P.export_patch(pol, P.Policy(pol.zones[:1], pol.commons, pol.ignore, pol.enforcement))
    assert patch.startswith("--- a/.remembra/zones.yml") and "-  pos:" in patch


def _policy(**zones: dict) -> P.Policy:
    return P.compile_policy({"zones": zones})


def test_diff_marks_every_loosening_kind():
    old = P.compile_policy(
        {
            "zones": {
                "pos": {
                    "include": ["src/pos/**"],
                    "protected": True,
                    "fail_closed": True,
                    "reserve_for": "codex",
                    "services": ["deploy:x"],
                },
                "gone": ["src/gone/**"],
                "held": ["src/held/**"],
                "m": {"include": ["src/m/**"], "mode": "exclusive"},
            },
            "commons": {"yarn.lock": "serialize"},
            "ignore": ["docs/**"],
        }
    )
    new = P.compile_policy(
        {
            "zones": {
                "pos": ["src/pos/**"],
                "held": {"include": ["src/held/**"], "exclude": ["src/held/tmp/**"]},
                "m": {"include": ["src/m/**"], "mode": "shared"},
                "added": ["src/added/**"],
            },
            "commons": {"yarn.lock": "plain", "src/**": "plain"},
            "ignore": ["docs/**", "src/**"],
            "enforcement": "observe",
        }
    )
    diff = P.diff_policy(old, new, live_claim_slugs=["held"], current_enforcement="enforce")
    loose = {(i.target, i.field) for i in diff.loosening_items}
    assert ("zone:gone", None) in loose
    assert ("zone:pos", "protected") in loose
    assert ("zone:pos", "fail_closed") in loose
    assert ("zone:pos", "reserve_for") in loose
    assert ("zone:pos", "services") in loose
    assert ("zone:held", "globs") in loose  # narrowed while claimed
    assert ("zone:m", "mode") in loose
    assert ("commons:yarn.lock", "kind") in loose
    assert ("commons:src/**", None) in loose
    assert ("ignore:src/**", None) in loose
    assert ("enforcement", None) in loose
    tight = {(i.target, i.field) for i in diff.items if not i.loosening}
    assert ("zone:added", None) in tight
    summary = diff.summary()
    assert len(summary) <= 500 and "loosening:" in summary and "remove gone" in summary


def test_tightening_changes_are_not_loosening():
    old = _policy(pos={"include": ["src/pos/**"]})
    new = _policy(pos={"include": ["src/pos/**", "src/pos2/**"], "protected": True, "mode": "exclusive"}, x=["x/**"])
    diff = P.diff_policy(old, new, current_enforcement="observe")
    assert diff.changed and not diff.loosening
    # narrowing an unclaimed, unprotected zone is not loosening
    narrowed = _policy(pos={"include": ["src/pos/a/**"]})
    assert not P.diff_policy(old, narrowed).loosening
    assert P.diff_policy(old, narrowed, live_claim_slugs=["pos"]).loosening
    # raising enforcement is tightening; omitting it changes nothing
    up = P.compile_policy({"enforcement": "enforce"})
    assert not P.diff_policy(P.Policy(), up, current_enforcement="observe").loosening
    assert not P.diff_policy(P.Policy(), P.Policy(), current_enforcement="observe").changed


def test_interim_holds_back_only_loosening_items():
    old = _policy(
        pos={"include": ["src/pos/**"], "protected": True}, gone=["src/gone/**"], child={"include": ["c/**"], "parent": "gone"}
    )
    new = P.compile_policy(
        {
            "zones": {"pos": {"include": ["src/pos/**"], "title": "Point of sale"}, "added": ["src/new/**"], "child": ["c/**"]},
            "ignore": ["x/**", "src/pos/**"],
        }
    )
    diff = P.diff_policy(old, new)
    interim = P.interim_policy(old, new, diff)
    pos = interim.zone("pos")
    assert pos is not None and pos.protected and pos.title == "Point of sale"  # title applied, protection kept
    assert interim.zone("gone") is not None  # removal held back
    assert interim.zone("added") is not None  # addition applied
    assert interim.ignore == ("x/**",)  # an ignore over a zone is held back; an unrelated one applies
    assert not P.diff_policy(old, interim).loosening


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/app/pos/**", "src/app/**/*.ts", True),
        ("src/a/*.ts", "src/a/*.css", False),
        ("src/app/pos", "src/app/pos/x.ts", True),
        ("src/{a,b}/**", "src/c/**", False),
        ("src/{a,b}/**", "src/b/x", True),
        ("**/*.lock", "yarn.lock", True),
        ("docs/**", "src/**", False),
        ("src/*/pos/**", "src/app/pos/cart.ts", True),
        ("src/App/**", "src/app/x", True),  # case-folded (conservative)
        ("a/b", "a/c", False),
    ],
)
def test_glob_overlap(a, b, expected):
    assert P.globs_overlap(a, b) is expected
    assert P.globs_overlap(b, a) is expected


def test_suggestions_are_deterministic_feature_folders():
    tree = {
        "name": ".",
        "files": 3,
        "children": [
            {
                "name": "src",
                "files": 0,
                "children": [
                    {
                        "name": "app",
                        "files": 1,
                        "children": [
                            {"name": "pos", "files": 5, "children": []},
                            {"name": "reports", "files": 2, "children": []},
                        ],
                    },
                    {"name": "lib", "files": 4, "children": []},
                ],
            },
            {"name": "docs", "files": 9, "children": []},
            {"name": "node_modules", "files": 900, "children": []},
            {"name": ".github", "files": 2, "children": []},
            {"name": "pos", "files": 1, "children": []},
        ],
    }
    zones = P.suggest_zones(tree)
    assert [(z.slug, z.include) for z in zones] == [
        ("pos", ("pos/**",)),
        ("app-pos", ("src/app/pos/**",)),
        ("reports", ("src/app/reports/**",)),
        ("lib", ("src/lib/**",)),
    ]
    assert P.suggest_zones(tree) == zones
    assert len(P.suggest_zones(tree, limit=2)) == 2
    with pytest.raises(P.PolicyError):
        P.suggest_zones({"name": "x"})


def test_policy_rows_round_trip():
    pol = P.parse_zones_yaml(ZONES_YML)
    rows = []
    for i, z in enumerate(pol.zones):
        rows.append(
            {
                "id": f"zn_{i}",
                "slug": z.slug,
                "title": z.title,
                "parent_id": None,
                "include_globs": list(z.include),
                "exclude_globs": [],
                "services": list(z.services),
                "command_patterns": list(z.commands),
                "mcp_tools": [],
                "mode": z.mode,
                "auto_claim": 1,
                "protected": 0,
                "fail_closed": 0,
                "archived_at": None,
                "builtin": 0,
            }
        )
    back = P.policy_from_rows(rows)
    assert {z.slug for z in back.zones} == set(pol.slugs)
    assert P.builtin_zone().protected and P.builtin_zone().include == tuple(S.CREW_POLICY_GLOBS)
