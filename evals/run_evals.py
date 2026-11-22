"""Run the scenario suite against the real graph (in-process MCP servers + mock alerts).

    uv run python -m evals.run_evals                     # deterministic metrics, current LLM backend
    uv run python -m evals.run_evals --judge             # + LLM-as-a-judge faithfulness (OpenRouter)
    uv run python -m evals.run_evals --langsmith         # also upload as a LangSmith experiment

Needs the database loaded (ingestion). Results are written to evals/results/<timestamp>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from evals.evaluators import metrics
from evals.evaluators.judge import faithfulness
from evals.harness import inprocess_gateway, set_scenario
from terramind.graph.state import turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import AppRuntime, open_runtime

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "datasets" / "scenarios.yaml"
RESULTS = ROOT / "results"


def load_cases(path: Path = DATASET) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return cases


async def run_case(rt: AppRuntime, case: dict[str, Any]) -> dict[str, Any]:
    set_scenario(case.get("scenario", "normal"))
    config: RunnableConfig = {
        "configurable": {"thread_id": f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"},
        "recursion_limit": 40,
        "run_name": f"eval:{case['id']}",
        "tags": ["eval"],
    }
    calls = start_call_log()
    path: list[str] = []
    timings: dict[str, float] = {}  # node -> total seconds (a node may run several times)
    started = time.perf_counter()
    async for mode, chunk in rt.graph.astream(
        turn_input([HumanMessage(content=case["message"])], f"eval-{case['id']}"),
        config,
        context=rt.context,
        stream_mode=["updates", "custom"],
    ):
        if mode == "updates" and isinstance(chunk, dict):
            path.extend(chunk.keys())
        elif isinstance(chunk, dict) and chunk.get("event") == "timing":
            timings[chunk["node"]] = round(timings.get(chunk["node"], 0.0) + chunk["seconds"], 2)
    elapsed = time.perf_counter() - started
    values = (await rt.graph.aget_state(config)).values
    final = values.get("final")
    plan = final.plan if final else None
    trail = next((c for c in values.get("candidates", []) if plan and c.trail_id == plan.trail_id), None)
    history = values.get("verdict_history", [])
    return {
        "id": case["id"],
        "path": path,
        "seconds": round(elapsed, 2),
        "timings": dict(sorted(timings.items(), key=lambda kv: -kv[1])),
        "plan": plan.model_dump(mode="json") if plan else None,
        "plan_trail_name": f"{trail.name} {trail.name_he or ''}" if trail else None,
        "plan_difficulty": trail.difficulty if trail else None,
        "attempts": values.get("attempts", 0),
        "vetoes": sum(1 for v in history if not v.approved),
        "tool_calls": calls,
        "summary": final.summary_md
        if final
        else (values["messages"][-1].content if values.get("messages") else ""),
        "evidence_ids": [e.chunk_id for e in values.get("evidence", [])],
        "data": {
            "request": values["request"].model_dump(mode="json", exclude_defaults=True)
            if values.get("request")
            else None,
            "plan": plan.model_dump(mode="json") if plan else None,
            "final": final.model_dump(mode="json") if final else None,
            "weather": values["weather"].model_dump(mode="json") if values.get("weather") else None,
            "evidence": [e.model_dump(mode="json") for e in values.get("evidence", [])],
            "candidates": [c.model_dump(mode="json") for c in values.get("candidates", [])],
            "verdicts": [v.model_dump(mode="json") for v in history],
        },
        "errors": [e.model_dump(mode="json") for e in values.get("errors", [])],
    }


def score(output: dict[str, Any], case: dict[str, Any]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for fn in metrics.ALL:
        scores.update(fn(output, case))
    return scores


async def run_local(judge: bool) -> list[dict[str, Any]]:
    cases = load_cases()
    rows: list[dict[str, Any]] = []
    async with inprocess_gateway() as gw, open_runtime(gateway=gw, persistent=False) as rt:
        for case in cases:
            out = await run_case(rt, case)
            s = score(out, case)
            if judge:
                s.update(await faithfulness(rt.context.llm, out))
            rows.append({"case": case["id"], "scores": s, "output": out})
            flat = " ".join(f"{k}={v}" for k, v in s.items() if isinstance(v, float))
            print(f"{case['id']:<28} {out['seconds']:>6.2f}s  {flat}")
            slow = " ".join(f"{k}={v:.1f}" for k, v in list(out["timings"].items())[:4])
            print(f"{'':<28} {'':>7}  slowest: {slow}  path: {len(out['path'])} steps")
    keys = sorted({k for r in rows for k, v in r["scores"].items() if isinstance(v, float)})
    print("-" * 80)
    means = []
    for k in keys:  # average only over the cases where the metric applies
        vals = [r["scores"][k] for r in rows if isinstance(r["scores"].get(k), float)]
        means.append(f"{k}={sum(vals) / len(vals):.3f}(n={len(vals)})")
    print("MEAN".ljust(36), " ".join(means))
    secs = sorted(
        r["output"]["seconds"] for r in rows if r["output"].get("plan") or r["output"]["data"]["final"]
    )
    if secs:
        print(f"LATENCY (planning cases)  median={secs[len(secs) // 2]:.1f}s  max={secs[-1]:.1f}s")
    totals: dict[str, float] = {}
    for r in rows:
        for k, v in r["output"]["timings"].items():
            totals[k] = totals.get(k, 0.0) + v
    print(
        "TIME BY NODE (all cases) ",
        " ".join(f"{k}={v:.0f}s" for k, v in sorted(totals.items(), key=lambda kv: -kv[1])),
    )
    RESULTS.mkdir(exist_ok=True)
    out_file = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"results: {out_file}")
    return rows


async def run_langsmith(dataset_name: str) -> None:
    """Same target and evaluators, recorded as a LangSmith experiment for side-by-side comparison."""
    from langsmith import Client, aevaluate

    client = Client()
    cases = load_cases()
    if not client.has_dataset(dataset_name=dataset_name):
        ds = client.create_dataset(dataset_name, description="TerraMind scenario suite")
        client.create_examples(
            dataset_id=ds.id,
            examples=[
                {"inputs": {"message": c["message"], "scenario": c.get("scenario", "normal")}, "outputs": c}
                for c in cases
            ],
        )
    async with inprocess_gateway() as gw, open_runtime(gateway=gw, persistent=False) as rt:

        async def target(inputs: dict[str, Any]) -> dict[str, Any]:
            return await run_case(rt, {"id": "ls", **inputs})

        def make(fn: Any) -> Any:
            def evaluator(outputs: dict[str, Any], reference_outputs: dict[str, Any]) -> list[dict[str, Any]]:
                return [{"key": k, "score": v} for k, v in fn(outputs, reference_outputs).items()]

            evaluator.__name__ = fn.__name__
            return evaluator

        await aevaluate(
            target,
            data=dataset_name,
            evaluators=[make(fn) for fn in metrics.ALL],
            experiment_prefix="terramind",
            max_concurrency=1,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--judge", action="store_true", help="add LLM-as-a-judge faithfulness (OpenRouter)")
    ap.add_argument("--langsmith", action="store_true", help="upload as a LangSmith experiment")
    ap.add_argument("--dataset", default="terramind-scenarios")
    args = ap.parse_args()
    if args.langsmith:
        asyncio.run(run_langsmith(args.dataset))
    else:
        asyncio.run(run_local(args.judge))


if __name__ == "__main__":
    main()
