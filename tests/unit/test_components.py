from __future__ import annotations

import json
import os
from datetime import date, time
from pathlib import Path
from typing import Any

from ingestion.osm_loader import build_query, parse_overpass
from ingestion.seed_knowledge import match_trail, read_tips
from mcp_servers.knowledge_server.hybrid import rrf_fuse, to_or_tsquery
from mcp_servers.weather_server.open_meteo import grid_key, parse_days, parse_snapshot, split_batch
from terramind.embeddings import HashEmbedder, to_pgvector
from terramind.graph.nodes.catalog_search import _planned_calls, fuse_candidates, search_args
from terramind.graph.nodes.route_planner import PlanDraft, build_plan
from terramind.regions import region_of, resolve_region
from terramind.schemas import (
    Difficulty,
    EvidenceChunk,
    GroupProfile,
    LegEstimate,
    PointOfInterest,
    TrailCandidate,
    TrailType,
    TripRequest,
)
from terramind.text import name_in_text

FIXTURE = Path(__file__).parents[1] / "fixtures" / "overpass_sample.json"


def test_overpass_parsing_skips_unnamed_and_unknown() -> None:
    trails, pois = parse_overpass(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert len(trails) == 4 and len(pois) == 7  # relation without geometry and the bakery are skipped
    kziv = next(t for t in trails if t.osm_id == 9000001)
    assert kziv.name_he == "שביל נחל כזיב" and kziv.wkt.startswith("MULTILINESTRING((")
    query = build_query((32.7, 35.1, 33.3, 35.9))
    assert 'relation["route"="hiking"]' in query
    assert "out geom;" in query and "out tags" not in query  # members are needed for the geometry


def test_name_matching_handles_hebrew_prefixes_and_generic_words() -> None:
    assert name_in_text("שביל נחל כזיב", "אני רוצה לטייל בנחל כזיב מחר")
    assert name_in_text("Mount Meron Summit Loop", "hike near Meron")
    assert not name_in_text("נחל עמוד", "hike nahal kziv")
    # Real OSM names carry extra words (marking colour, destination): one distinctive word is enough.
    assert name_in_text("נחל כזיב - שביל אדום", "אני רוצה לטייל בנחל כזיב מחר, מסלול מים")
    assert name_in_text("Nahal Kziv red trail", "hike Nahal Kziv tomorrow")
    assert not name_in_text("שביל אדום למעיין", "אני רוצה מסלול מים אדום")  # only generic words shared
    assert name_in_text("נחל אל על", "טיול בנחל אל על")  # short distinctive words: all must appear
    assert not name_in_text("נחל אל על", "טיול על הר מירון")
    trails = [("a", ["נחל עמוד", "Nahal Amud"]), ("k", ["שביל נחל כזיב", "Nahal Kziv Trail"])]
    assert match_trail("Nahal Kziv", trails) == "k"
    assert match_trail("Nahal Iyun", trails) is None  # the bug the first ingestion run exposed


def test_tips_file_is_valid() -> None:
    tips = read_tips(Path(__file__).parents[2] / "ingestion" / "data" / "tips.jsonl")
    assert len(tips) >= 20 and {t.lang for t in tips} == {"he", "en"}
    assert len({t.chunk_id for t in tips}) == len(tips)


def test_regions() -> None:
    assert resolve_region("רמת הגולן") and resolve_region("the Golan").key == "golan"  # type: ignore[union-attr]
    assert resolve_region("Mars") is None
    assert region_of((33.05, 35.2)).key == "upper_galilee"  # type: ignore[union-attr]


async def test_hash_embedder_is_normalized_and_lexical() -> None:
    e = HashEmbedder(dim=64)
    a, b, c = await e.embed(["מסלול מים", "מסלול מים בגליל", "castle ruins"])
    assert abs(sum(x * x for x in a) - 1) < 1e-6
    dot = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))  # noqa: E731
    assert dot(a, b) > dot(a, c)
    assert to_pgvector([0.5, -1.0]).startswith("[0.500000,")


def test_rrf_and_or_tsquery() -> None:
    c = lambda i: EvidenceChunk(chunk_id=f"c{i}", trail_id=None, text="", source="", score=0)  # noqa: E731
    fused = rrf_fuse([[c(1), c(2), c(3)], [c(3), c(1)]], k=3)
    assert [x.chunk_id for x in fused] == ["c1", "c3", "c2"]
    assert to_or_tsquery("מסלול מים, מים!") == "מסלול | מים"
    assert to_or_tsquery("!!") is None


def test_candidate_fusion_rewards_semantic_match() -> None:
    def t(tid: str, r: int) -> TrailCandidate:
        return TrailCandidate(
            trail_id=tid, name=tid, region="r", length_km=5, trailhead=(33, 35), spatial_rank=r
        )

    chunks = [EvidenceChunk(chunk_id="k", trail_id="b", text="", source="", score=1)]
    assert [x.trail_id for x in fuse_candidates([t("a", 1), t("b", 2)], chunks)] == ["b", "a"]


def test_search_args_map_request_and_relax() -> None:
    req = TripRequest(
        region="Golan",
        trail_types=[TrailType.WATER, TrailType.LOOP],
        max_duration_hours=4,
        difficulty=Difficulty.EASY,
        semantic_query="q",
    )
    strict = search_args(req, ["x"], relaxed=False)
    assert strict["tags"] == ["water"] and strict["loop_only"] and strict["max_km"] == 10.5
    loose = search_args(req, ["x"], relaxed=True)
    assert loose["tags"] == [] and not loose["loop_only"] and loose["exclude_ids"] == ["x"]


def test_day_hike_cap_holds_whatever_the_model_asks() -> None:
    """Multi-day trails (Golan Trail, 125 km) never reach agent 3 as day-hike candidates."""
    req = TripRequest(region="Golan", semantic_query="q")

    def max_km(model_args: dict[str, Any] | None) -> float:
        calls = [{"name": "search_trails", "args": model_args}] if model_args is not None else []
        return dict(_planned_calls(calls, req, ["x"], relaxed=False, day_cap_km=25))["search_trails"][
            "max_km"
        ]

    assert max_km(None) == 25  # code-built fallback
    assert max_km({"region": "Golan"}) == 25  # the model left it out
    assert max_km({"region": "Golan", "max_km": 120}) == 25  # the model asked for too much
    assert max_km({"region": "Golan", "max_km": 8}) == 8  # a tighter user limit is kept


def test_build_plan_orders_stops_along_the_trail() -> None:
    trail = TrailCandidate(
        trail_id="t", name="T", region="r", length_km=7, trailhead=(33, 35.2), is_loop=False
    )
    pois = [
        PointOfInterest(poi_id=f"p{i}", name=f"p{i}", category=cat, location=(33, 35.2), distance_m=d)
        for i, (cat, d) in enumerate([("spring", 2500), ("viewpoint", 900), ("parking", 0)])
    ]
    req = TripRequest(
        region="r", trip_date=date(2026, 12, 5), semantic_query="q", group=GroupProfile(pace_kmh=3.5)
    )
    draft = PlanDraft(trail_id="t", title="T", poi_ids=[], place_ids=[], rationale="r")
    p = build_plan(draft, trail, pois, (33.01, 35.25), req, time(8), [])
    times = [s.arrive_at for s in p.stops]
    assert p.start_time == time(8) and times == sorted(times)  # type: ignore[type-var]
    assert [s.kind for s in p.stops] == ["trailhead", "viewpoint", "water", "end"]  # parking is not a stop
    early = build_plan(draft, trail, pois, None, req, time(6, 35), [])  # the start agent 3 approved
    assert early.start_time == time(6, 35) and early.end_time < p.end_time
    assert p.drive_minutes is None and p.depart_at is None  # no starting point, no drive
    home = req.model_copy(update={"origin": (32.79, 34.99), "origin_label": "חיפה"})
    leg = LegEstimate(
        mode="drive", distance_km=61.4, minutes=55, method="routing_engine", route=[(32.79, 34.99)]
    )
    driven = build_plan(draft, trail, pois, None, home, time(8), [], leg)
    assert (driven.drive_minutes, driven.depart_at, driven.origin_label) == (55, time(7, 5), "חיפה")


def test_open_meteo_parsing() -> None:
    d = date(2026, 12, 5)
    s = parse_snapshot(
        "t",
        d,
        {
            "daily": {
                "temperature_2m_max": [18.5],
                "precipitation_probability_max": [70],
                "sunset": ["2026-12-05T16:39"],
            },
            "hourly": {"time": ["2026-12-05T14:00"], "precipitation_probability": [80]},
        },
    )
    assert s.precip_prob_max_pct == 70 and s.sunset is not None and s.sunset.hour == 16
    assert s.hourly_precip_prob == {14: 80}


def test_open_meteo_batch_splits_points_and_days() -> None:
    """The pipeline asks for many points at once: a list back, each point with every day."""
    point = {
        "daily": {"time": ["2026-12-05", "2026-12-06"], "temperature_2m_max": [18, 21]},
        "hourly": {"time": ["2026-12-05T09:00", "2026-12-06T09:00"], "precipitation_probability": [10, 60]},
    }
    answers = split_batch([point, point])
    days = parse_days(answers[0])
    assert len(answers) == 2 and [d.temp_max_c for d in days] == [18, 21]
    assert days[1].for_date == date(2026, 12, 6) and days[1].hourly_precip_prob == {9: 60}
    assert split_batch(point) == [point]  # a one-point request answers with an object
    assert grid_key(33.0512, 35.1812) == grid_key(33.0488, 35.1788)  # same ~1 km cell


async def test_overpass_falls_back_to_a_mirror_on_502(tmp_path: Path, monkeypatch: object) -> None:
    import httpx
    import respx

    from ingestion import osm_loader
    from terramind.config import Settings

    monkeypatch.setattr(osm_loader, "CACHE_DIR", tmp_path)  # type: ignore[attr-defined]
    s = Settings(overpass_urls=["https://main.example/api", "https://mirror.example/api"])
    with respx.mock() as m:
        m.post("https://main.example/api").mock(return_value=httpx.Response(502))
        m.post("https://mirror.example/api").mock(
            return_value=httpx.Response(200, json={"elements": [{"id": 1}]})
        )
        data = await osm_loader.fetch_overpass(s, refresh=True)
    assert data["elements"] == [{"id": 1}]
    assert any(p.name.startswith("overpass_") for p in tmp_path.iterdir())  # noqa: ASYNC240


async def test_wrong_password_explains_the_port_clash() -> None:
    import pytest

    from terramind.db import DatabaseSetupError, connect

    with pytest.raises(DatabaseSetupError, match=r"(DIFFERENT PostgreSQL|Cannot reach)"):
        await connect("postgresql://terramind:wrong@localhost:5432/terramind", connect_timeout=3)


async def test_region_falls_back_to_tiles_and_dedupes(tmp_path: Path, monkeypatch: object) -> None:
    from ingestion import osm_loader
    from terramind.config import Settings

    calls: list[tuple[float, ...]] = []

    async def fake_fetch(
        settings: Settings, *, refresh: bool, bbox: tuple[float, float, float, float]
    ) -> dict:  # type: ignore[type-arg]
        calls.append(bbox)
        if bbox == settings.region_bbox:
            raise RuntimeError("all mirrors busy")
        return {"elements": [{"type": "relation", "id": 1}, {"type": "node", "id": len(calls)}]}

    monkeypatch.setattr(osm_loader, "fetch_overpass", fake_fetch)  # type: ignore[attr-defined]
    data = await osm_loader.fetch_region(
        Settings(region_bbox=(32.0, 35.0, 33.0, 36.0)), refresh=True, tiles=1
    )
    assert len(calls) == 1 + 4  # full region failed, then a 2x2 grid
    assert sum(1 for el in data["elements"] if el["type"] == "relation") == 1  # shared relation kept once
    assert osm_loader.split_bbox((32.0, 35.0, 33.0, 36.0), 2)[3] == (32.5, 35.5, 33.0, 36.0)


async def test_stream_text_handles_anthropic_block_chunks(monkeypatch: object) -> None:
    from collections.abc import AsyncIterator

    from langchain_core.messages import AIMessageChunk

    from terramind.llm import client

    class FakeModel:
        async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[AIMessageChunk]:
            yield AIMessageChunk(content=[{"type": "thinking", "thinking": "planning...", "index": 0}])
            yield AIMessageChunk(content=[{"type": "text", "text": "שלום ", "index": 1}])
            yield AIMessageChunk(content="עולם")

    monkeypatch.setattr(client, "get_chat_model", lambda *a, **k: FakeModel())  # type: ignore[attr-defined]
    out = [t async for t in client.ClaudeLLM().stream_text("presenter", [])]
    assert "".join(out) == "שלום עולם"  # text blocks kept, thinking dropped


async def test_bge_load_survives_caller_timeouts(monkeypatch: object) -> None:
    """Regression: a timed-out call used to discard the loaded model, so loading never finished."""
    import asyncio
    import contextlib
    import sys
    import time
    import types

    from terramind.embeddings import BgeEmbedder

    loads: list[float] = []

    class SlowModel:
        def __init__(self, name: str) -> None:
            loads.append(time.monotonic())
            time.sleep(0.3)  # loading takes longer than the caller is willing to wait

        def encode(self, texts: list[str], **_: object) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = SlowModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)  # type: ignore[attr-defined]
    emb = BgeEmbedder(dim=2)
    for _ in range(2):
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(emb.embed(["x"]), timeout=0.05)
    assert await emb.embed(["x"]) == [[1.0, 0.0]]
    assert len(loads) == 1  # loaded exactly once despite two cancelled callers


async def test_structured_output_reuses_the_method_that_worked(monkeypatch: Any) -> None:
    """A method the gateway rejects is tried once per process, not on every call."""
    from pydantic import BaseModel as _Base

    from terramind.llm import client as client_mod

    class Out(_Base):
        x: int

    tried: list[str] = []

    class FakeModel:
        def with_structured_output(self, schema: type[Out], method: str) -> Any:
            class Runner:
                async def ainvoke(self, msgs: Any, config: Any = None) -> Out:
                    tried.append(method)
                    if method == "function_calling":
                        raise RuntimeError("400 tool_choice not supported")
                    return Out(x=1)

            return Runner()

    monkeypatch.setattr(client_mod, "get_chat_model", lambda task: FakeModel())
    monkeypatch.setattr(client_mod, "model_id_for", lambda task: "m")
    llm = client_mod.ClaudeLLM()
    for _ in range(3):
        assert (await llm.structured("request_analyst", Out, [])).x == 1
    assert tried == ["function_calling", "json_schema", "json_schema", "json_schema"]


def test_langsmith_is_exported_only_with_a_real_key(monkeypatch: Any) -> None:
    """LangSmith reads os.environ; .env values reach it only through configure_langsmith."""
    from terramind.config import Settings
    from terramind.observability import configure_langsmith

    for var in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"):
        monkeypatch.delenv(var, raising=False)
    placeholder = Settings(langsmith_tracing=True, langsmith_api_key="lsv2_...")
    assert configure_langsmith(placeholder) is False and os.environ["LANGSMITH_TRACING"] == "false"

    monkeypatch.delenv("LANGSMITH_TRACING")
    real = Settings(langsmith_tracing=True, langsmith_api_key="lsv2_pt_" + "x" * 40, langsmith_project="p")
    assert configure_langsmith(real) is True
    assert (os.environ["LANGSMITH_TRACING"], os.environ["LANGSMITH_PROJECT"]) == ("true", "p")


def test_opening_hours_reader() -> None:
    """Common OSM formats are read; anything else is 'unknown', never guessed open."""
    from terramind.opening_hours import is_open

    fri, sat, sun = date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 29)
    week = "Su-Th 09:00-17:00; Fr 09:00-14:00; Sa off"
    assert is_open(week, sun, time(12), 60) == "open"
    assert is_open(week, fri, time(13, 30), 60) == "closed"  # would still be there at 14:30
    assert is_open(week, sat, time(12)) == "closed"
    assert is_open("Mo-Fr 12:00-15:00,18:00-23:00", fri, time(16)) == "closed"
    assert is_open("Mo-Fr 12:00-15:00,18:00-23:00", fri, time(19), 90) == "open"
    assert is_open("Fr-Sa 20:00-02:00", sat, time(1)) == "open"  # Friday's night continues
    assert is_open("24/7", sat, time(23)) == "open"
    assert is_open("10:00-18:00", sat, time(11)) == "open"  # no days = every day
    assert is_open("Mo-Fr 10:00-18:00", sat, time(11)) == "closed"  # unmentioned day = closed (OSM)
    assert is_open("Mo-Fr 10:00-18:00; PH off", sun, time(11)) == "unknown"  # holidays: not read
    assert is_open(None, sun, time(11)) == "unknown"


def test_day_plan_times_places_and_drops_what_does_not_fit() -> None:
    """Coffee on the way moves the departure earlier; after the hike, a place closed on Saturday,
    a viewpoint after sunset and a place past the day's hours are dropped, each with the reason."""
    from datetime import datetime

    from terramind.day_plan import add_places, leg_pairs, split_before_after
    from terramind.schemas import NearbyPlace, RoutePlan, Stop

    sat = date(2026, 11, 28)
    plan = RoutePlan(
        plan_id="p",
        trail_id="t",
        title="T",
        walk_km=5,
        walk_minutes=120,
        stops=[
            Stop(order=0, name="start", kind="trailhead", location=(33.0, 35.2), arrive_at=time(8)),
            Stop(order=1, name="end", kind="end", location=(33.0, 35.2), arrive_at=time(10, 30)),
        ],
        start_time=time(8),
        end_time=time(10, 30),
        origin=(32.8, 35.0),
        drive_minutes=60,
        depart_at=time(7),
    )

    def place(pid: str, cat: str, hours: str | None) -> NearbyPlace:
        return NearbyPlace(
            poi_id=pid, name=pid, category=cat, location=(33.0, 35.21), distance_km=1, opening_hours=hours
        )

    cart = place("cart", "coffee_cart", "24/7")
    closed = place("grill", "restaurant", "Su-Th 12:00-22:00; Sa off")
    winery = place("winery", "winery", "Sa 10:00-16:00")
    view = place("view", "viewpoint", None)
    museum = place("museum", "museum", None)
    before, after = split_before_after([cart, closed, winery, view, museum], on_way_ids={"cart"})
    assert before == [cart] and after == [closed, winery, view, museum]
    assert leg_pairs(before, after, has_origin=True)[:2] == [("origin", "cart"), ("cart", "trailhead")]

    legs = {
        ("origin", "cart"): LegEstimate(mode="drive", distance_km=40, minutes=40),
        ("cart", "trailhead"): LegEstimate(mode="drive", distance_km=10, minutes=15),
    }
    req = TripRequest(region="r", trip_date=sat, semantic_query="q", day_hours=5)
    out = add_places(plan, before, after, legs, req, sunset=datetime(2026, 11, 28, 11, 40))
    names = [s.name for s in out.stops]
    assert names[0] == "cart" and out.stops[0].arrive_at == time(7, 25)  # 08:00 - 15 drive - 20 coffee
    assert out.depart_at == time(6, 45) and out.drive_minutes == 55
    assert "winery" in names and "grill" not in names and "view" not in names
    assert any("grill" in d and "סגור" in d for d in out.dropped_places)
    assert any("view" in d and "השקיעה" in d for d in out.dropped_places)
    assert any("museum" in d and "5 שעות" in d for d in out.dropped_places)  # 07:25 + 5h = 12:25
    assert [s.order for s in out.stops] == list(range(len(out.stops)))
    assert out.day_end_time is not None and out.day_end_time > out.end_time

    # A place opening soon after we arrive is waited for, not dropped.
    lunch = place("lunch", "restaurant", "Sa 11:00-22:00")
    waited = add_places(plan, [], [lunch], {}, req, sunset=None)
    stop = next(s for s in waited.stops if s.place_id == "lunch")
    assert stop.arrive_at == time(11, 0) and stop.open_state == "open" and "ממתינים" in (stop.note or "")


def test_best_start_moves_the_hike_so_places_are_open() -> None:
    """A later start is taken only when it keeps more places, is safe, and does not leave home before
    the time the user gave; nothing usable -> the start agent 3 approved."""
    from datetime import datetime, timedelta

    from terramind.day_plan import best_start
    from terramind.schemas import RoutePlan, Stop

    def day_at(at: time) -> RoutePlan:
        # A restaurant that only fits when the hike starts at 10:00 or later; the drive is an hour.
        stops = [
            Stop(order=0, name="a", kind="trailhead", location=(33, 35)),
            Stop(order=1, name="b", kind="end", location=(33, 35)),
        ]
        if at >= time(10):
            stops.append(Stop(order=2, name="r", kind="food", location=(33, 35), place_id="r"))
        depart = (datetime.combine(date.min, at) - timedelta(hours=1)).time()
        return RoutePlan(
            plan_id="p", trail_id="t", title="T", walk_km=2, walk_minutes=40, stops=stops,
            start_time=at, end_time=at, depart_at=depart,
        )  # fmt: skip

    def always(_: time) -> bool:
        return True

    moved = best_start(day_at, time(8), always, time(8))
    assert moved.start_time == time(10) and "10:00" in (moved.start_note or "")
    unsafe_until_1045 = best_start(day_at, time(8), lambda t: t >= time(10, 45), time(8))
    assert unsafe_until_1045.start_time == time(10, 45)
    # "נצא ב-9" with an hour's drive: the earliest start is 10:00, and nothing leaves before 09:00.
    leaving = best_start(day_at, time(10), always, time(8), leave_home=time(9))
    assert leaving.start_time == time(10) and leaving.start_note is None
    assert best_start(day_at, time(8), lambda _: False, time(8)).start_time == time(8)
