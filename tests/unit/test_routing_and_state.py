from __future__ import annotations

from datetime import date, time

from terramind.graph.builder import SERDE, allowed_msgpack_types
from terramind.graph.routing import route_after_critic, route_after_grader, route_supervisor
from terramind.graph.state import RESET, append_or_reset, per_turn_reset, union_or_reset
from terramind.schemas import (
    EvidenceChunk,
    FinalAnswer,
    RoutePlan,
    SafetyVerdict,
    Stop,
    TrailCandidate,
    TripRequest,
)


def cand(tid: str = "t1") -> TrailCandidate:
    return TrailCandidate(trail_id=tid, name=tid, region="r", length_km=5, trailhead=(33.0, 35.3))


def chunk(i: int) -> EvidenceChunk:
    return EvidenceChunk(chunk_id=f"kc-{i:016x}", trail_id=None, text="x", source="s", score=1)


def test_reducers_union_and_reset() -> None:
    assert union_or_reset(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert union_or_reset(["a"], RESET) == []
    assert append_or_reset([1], [2]) == [1, 2]
    assert append_or_reset([1, 2], RESET) == []
    assert append_or_reset(None, [3]) == [3]


def test_per_turn_reset_clears_loop_state() -> None:
    reset = per_turn_reset()
    assert reset["rejected_trail_ids"] is RESET and reset["verdict_history"] is RESET
    assert reset["attempts"] == 0 and reset["turn_classified"] is False


def test_supervisor_routes() -> None:
    base = {"messages": [], "user_id": "u"}
    assert route_supervisor({**base, "intent": "smalltalk"}) == "ask_clarification"  # type: ignore[typeddict-item]
    assert route_supervisor({**base, "intent": "new_plan"}) == "intake"  # type: ignore[typeddict-item]
    req = TripRequest(semantic_query="q", missing_fields=["region"])
    assert (
        route_supervisor({**base, "intent": "new_plan", "intake_done": True, "request": req})
        == "ask_clarification"
    )  # type: ignore[typeddict-item]
    req_ok = TripRequest(region="Golan", semantic_query="q")
    assert (
        route_supervisor({**base, "intent": "new_plan", "intake_done": True, "request": req_ok})
        == "researcher"
    )  # type: ignore[typeddict-item]
    final = FinalAnswer(summary_md="", plan=None)
    assert route_supervisor({**base, "intent": "question", "final": final}) == "explainer"  # type: ignore[typeddict-item]


def test_grader_routes() -> None:
    base = {"messages": [], "user_id": "u"}
    assert route_after_grader({**base, "candidates": [], "research_rounds": 1}) == "researcher"  # type: ignore[typeddict-item]
    assert route_after_grader({**base, "candidates": [], "research_rounds": 2}) == "safe_fallback"  # type: ignore[typeddict-item]
    few = {**base, "candidates": [cand()], "evidence": [chunk(1)], "research_rounds": 1}
    assert route_after_grader({**few, "rewritten_query": "better"}) == "researcher"  # type: ignore[typeddict-item]
    assert route_after_grader(few) == "planner"  # type: ignore[arg-type]  # no rewrite suggested
    assert route_after_grader({**few, "rewritten_query": "x", "research_rounds": 2}) == "planner"  # type: ignore[typeddict-item]


def test_critic_routes() -> None:
    base = {"messages": [], "user_id": "u"}
    ok = SafetyVerdict(approved=True)
    timing = SafetyVerdict(approved=False, replan_scope="timing")
    trail = SafetyVerdict(approved=False, replan_scope="trail")
    assert route_after_critic({**base, "verdict": ok, "attempts": 1}) == "explainer"  # type: ignore[typeddict-item]
    assert route_after_critic({**base, "verdict": timing, "attempts": 1}) == "planner"  # type: ignore[typeddict-item]
    # Trail veto: replan from the remaining candidates; search again only when none is left.
    left = {**base, "candidates": [cand("a"), cand("b")], "rejected_trail_ids": ["a"]}
    assert route_after_critic({**left, "verdict": trail, "attempts": 2}) == "planner"  # type: ignore[typeddict-item]
    gone = {**left, "rejected_trail_ids": ["a", "b"]}
    assert route_after_critic({**gone, "verdict": trail, "attempts": 2}) == "researcher"  # type: ignore[typeddict-item]
    assert route_after_critic({**base, "verdict": trail, "attempts": 3}) == "safe_fallback"  # type: ignore[typeddict-item]


def test_serde_roundtrip_registered_types() -> None:
    assert ("terramind.schemas.plan", "RoutePlan") in allowed_msgpack_types()
    stops = [
        Stop(order=0, name="a", kind="trailhead", location=(33, 35)),
        Stop(order=1, name="b", kind="end", location=(33, 35)),
    ]
    plan = RoutePlan(
        plan_id="p",
        trail_id="t",
        title="x",
        stops=stops,
        walk_km=5,
        walk_minutes=90,
        start_time=time(8),
        end_time=time(10),
    )
    req = TripRequest(region="Golan", trip_date=date(2026, 12, 1), semantic_query="q")
    for obj in (plan, req, SafetyVerdict(approved=True)):
        kind, data = SERDE.dumps_typed(obj)
        assert SERDE.loads_typed((kind, data)) == obj
