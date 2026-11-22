from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any

from ingestion.osm_loader import build_query, parse_overpass
from ingestion.seed_knowledge import match_trail, read_tips
from mcp_servers.knowledge_server.hybrid import rrf_fuse, to_or_tsquery
from mcp_servers.weather_server.open_meteo import parse_snapshot
from terramind.embeddings import HashEmbedder, to_pgvector
from terramind.graph.nodes.catalog_search import fuse_candidates, search_args
from terramind.graph.nodes.route_planner import PlanDraft, build_plan
from terramind.regions import region_of, resolve_region
from terramind.schemas import (
    Difficulty,
    EvidenceChunk,
    GroupProfile,
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
    draft = PlanDraft(trail_id="t", title="T", rationale="r")
    p = build_plan(draft, trail, pois, (33.01, 35.25), req, time(8), [])
    times = [s.arrive_at for s in p.stops]
    assert p.start_time == time(8) and times == sorted(times)  # type: ignore[type-var]
    assert [s.kind for s in p.stops] == ["trailhead", "viewpoint", "water", "end"]  # parking is not a stop
    early = build_plan(draft, trail, pois, None, req, time(6, 35), [])  # the start agent 3 approved
    assert early.start_time == time(6, 35) and early.end_time < p.end_time


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
                    if method == "json_schema":
                        raise RuntimeError("400 unsupported response_format")
                    return Out(x=1)

            return Runner()

    monkeypatch.setattr(client_mod, "get_chat_model", lambda task: FakeModel())
    monkeypatch.setattr(client_mod, "model_id_for", lambda task: "m")
    llm = client_mod.ClaudeLLM()
    for _ in range(3):
        assert (await llm.structured("request_analyst", Out, [])).x == 1
    assert tried == ["json_schema", "function_calling", "function_calling", "function_calling"]
