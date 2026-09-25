"""Reducer vectors (§4.4): the Python reference reducer satisfies every vector, and the runner catches wrong reducers."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from remembra.crew import reducer as R
from remembra.crew import schemas as S
from tests.crew.vectors.loader import check_assertions, reducer_vectors, run_reducer_vectors

VECTORS = reducer_vectors()


def test_there_are_enough_vectors_and_each_has_assertions() -> None:
    assert len(VECTORS) >= 18
    for v in VECTORS:
        assert v["expect"], v["name"]
        assert v["description"], v["name"]


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_reference_reducer_satisfies_vector(vector: dict[str, Any]) -> None:
    state = R.reduce(vector["snapshot"], vector["frames"])
    state = json.loads(json.dumps(state))  # the state must be plain JSON (compared with the TS reducer)
    assert check_assertions(state, vector["expect"]) == []


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: v["name"])
def test_vector_inputs_follow_the_contract(vector: dict[str, Any]) -> None:
    assert S.validate(vector["snapshot"], S.SNAPSHOT) == []
    if not vector.get("events_validate", True):
        return
    for frame in vector["frames"]:
        if frame["type"] == "snapshot":
            assert S.validate(frame["data"], S.SNAPSHOT) == []
        else:
            assert S.validate_ws_frame(frame) == [], frame


def test_runner_reports_nothing_for_the_reference_reducer() -> None:
    assert run_reducer_vectors(R.reduce) == {}


def test_runner_catches_a_reducer_that_ignores_events() -> None:
    failures = run_reducer_vectors(lambda snap, frames: R.from_snapshot(snap))
    assert len(failures) >= len(VECTORS) - 2  # only snapshot_init / other_crew_ignored can pass


def test_runner_catches_a_reducer_that_applies_duplicates() -> None:
    def sloppy(snapshot: Any, frames: list[Any]) -> Any:
        state = R.from_snapshot(snapshot)
        for f in frames:
            if f["type"] == "crew.event" and f["data"]["seq"] <= state["last_seq"]:
                state["last_seq"] = f["data"]["seq"] - 1  # re-applies replays
            state = R.apply_frame(state, f)
        return state

    assert "duplicates_and_old_seq_ignored" in run_reducer_vectors(sloppy)


def test_runner_catches_a_reducer_that_lets_presence_change_state() -> None:
    def leaky(snapshot: Any, frames: list[Any]) -> Any:
        state = R.from_snapshot(snapshot)
        for f in frames:
            state = R.apply_frame(state, f)
            if f["type"] == "presence":
                for lane in f["lanes"]:
                    if lane["session_id"] in state["sessions"] and f["crew_id"] == state["crew"]["id"]:
                        state["sessions"][lane["session_id"]]["state"] = lane["state"]
        return state

    assert "presence_overlay" in run_reducer_vectors(leaky)


def test_handlers_cover_exactly_the_l0_event_set() -> None:
    assert set(R._HANDLERS) == set(S.L0_EVENT_TYPES)


def test_reduce_is_pure_and_does_not_mutate_inputs() -> None:
    for v in VECTORS:
        before = copy.deepcopy(v)
        a = R.reduce(v["snapshot"], v["frames"])
        b = R.reduce(v["snapshot"], v["frames"])
        assert a == b
        assert v == before, v["name"]


def test_snapshot_frame_returns_a_fresh_state_and_clears_resync() -> None:
    v = next(x for x in VECTORS if x["name"] == "gap_sets_resync")
    state = R.reduce(v["snapshot"], v["frames"])
    assert state["needs_resync"] is True
    fresh = R.apply_frame(state, {"type": "snapshot", "data": v["snapshot"]})
    assert fresh is not state
    assert fresh["needs_resync"] is False
    assert fresh["last_seq"] == v["snapshot"]["as_of_seq"]


def test_unknown_frames_are_ignored() -> None:
    v = VECTORS[0]
    state = R.from_snapshot(v["snapshot"])
    before = copy.deepcopy(state)
    for frame in (
        {"type": "crew.subscribed", "crew_id": "x", "since_seq": 0, "replayed": 0},
        {"type": "crew.summary", "crews": []},
        {"type": "whatever"},
    ):
        state = R.apply_frame(state, frame)
    assert state == before


def test_baton_refs_are_capped() -> None:
    v = VECTORS[0]
    state = R.from_snapshot(v["snapshot"])
    for i in range(R.BATON_REFS_KEEP + 5):
        ev = {
            "seq": 11 + i,
            "crew_id": state["crew"]["id"],
            "type": "baton.ref_created",
            "v": 1,
            "moment": False,
            "refs": {"session_id": "cs_a"},
            "actor": {"kind": "session", "id": "cs_a"},
            "payload": {"ref": f"refs/remembra/baton/T-1/{i}", "task_id": None, "dirty_files": 1, "unpushed": 0},
        }
        state = R.apply_event(state, ev)
    assert len(state["baton_refs"]) == R.BATON_REFS_KEEP
    assert "refs/remembra/baton/T-1/0" not in state["baton_refs"]
    assert f"refs/remembra/baton/T-1/{R.BATON_REFS_KEEP + 4}" in state["baton_refs"]
