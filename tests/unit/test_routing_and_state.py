"""Which agent runs next: pure functions of the state."""

from __future__ import annotations

from datetime import time

from terramind.graph.routing import after_enrichment, after_request_analyst
from terramind.graph.state import TerraMindState, approved, rejected_ids
from terramind.schemas import CandidateAssessment, TrailCandidate, TripRequest


def cand(tid: str) -> TrailCandidate:
    return TrailCandidate(trail_id=tid, name=tid, region="r", length_km=5, trailhead=(33.0, 35.3))


def verdict(tid: str, ok: bool) -> CandidateAssessment:
    return CandidateAssessment(trail_id=tid, approved=ok, start_time=time(8))


def state(**kw: object) -> TerraMindState:
    return {"messages": [], "assessments": [], "errors": [], **kw}  # type: ignore[typeddict-item]


def test_missing_region_ends_the_run_after_agent_1() -> None:
    assert after_request_analyst(state(clarification="Which area?")) == "__end__"
    assert after_request_analyst(state(clarification=None)) == "catalog_search"


def test_a_safe_candidate_goes_to_planning() -> None:
    s = state(candidates=[cand("a"), cand("b")], assessments=[verdict("a", False), verdict("b", True)])
    assert after_enrichment(s) == "route_planner"


def test_all_vetoed_searches_again_once_then_explains() -> None:
    vetoed = [verdict("a", False)]
    assert (
        after_enrichment(state(candidates=[cand("a")], assessments=vetoed, search_rounds=1))
        == "catalog_search"
    )
    assert after_enrichment(state(candidates=[cand("a")], assessments=vetoed, search_rounds=2)) == "presenter"


def test_approved_follows_search_ranking_and_ignores_older_rounds() -> None:
    s = state(
        candidates=[cand("b"), cand("c")],
        assessments=[verdict("x", True), verdict("c", True), verdict("b", True), verdict("a", False)],
    )
    assert [a.trail_id for a in approved(s)] == ["b", "c"]  # "x" was approved in an earlier round
    assert rejected_ids(s) == ["a"]


def test_follow_up_scope_is_decided_by_what_changed() -> None:
    """A change to the day re-plans the trails agent 3 approved; a change to what is searched or
    checked for safety searches again; no previous plan means a new search."""
    from terramind.graph.scope import decide_scope
    from terramind.schemas import GroupProfile, PreviousTurn, RoutePlan, Stop

    base = TripRequest(region="Golan", semantic_query="q")
    plan = RoutePlan(
        plan_id="p",
        trail_id="t",
        title="T",
        walk_km=3,
        walk_minutes=60,
        stops=[
            Stop(order=0, name="a", kind="trailhead", location=(33, 35)),
            Stop(order=1, name="b", kind="end", location=(33, 35)),
        ],
        start_time=time(8),
        end_time=time(9),
    )
    prev = PreviousTurn(request=base, plan=plan)

    def scope(new: TripRequest, change: str = "edit") -> str:
        return decide_scope(prev, new, change)[0]  # type: ignore[arg-type]

    assert scope(base.model_copy(update={"want_places": ["restaurant"]})) == "replan"
    assert scope(base.model_copy(update={"day_hours": 4})) == "replan"
    assert scope(base.model_copy(update={"start_time": time(9)})) == "search"  # daylight is re-checked
    assert scope(base.model_copy(update={"group": GroupProfile(children=2, min_child_age=5)})) == "search"
    assert scope(base, "different_trail") == "search"
    assert scope(base, "new_trip") == "search"
    assert decide_scope(None, base, "edit")[0] == "search"
    reason = decide_scope(prev, base.model_copy(update={"day_hours": 4}), "edit")[1]
    assert "אורך היום" in reason
