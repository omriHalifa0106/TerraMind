"""One-command demo in the terminal: the whole graph in-process (no servers to start).

    uv run python -m scripts.demo_cli "מסלול מים בגליל העליון מחר" --scenario kziv_closed

Needs only the database (docker compose up -d db + ingestion). Uses Claude when
OPENROUTER_API_KEY is set, otherwise the offline HeuristicLLM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import StreamMode

from evals.harness import inprocess_gateway, set_scenario
from terramind.graph.state import turn_input
from terramind.mcp_client.client import start_call_log
from terramind.runtime import open_runtime

DIM, BOLD, RED, GREEN, END = "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[0m"


async def run(message: str, scenario: str) -> None:
    set_scenario(scenario)
    async with inprocess_gateway() as gw, open_runtime(gateway=gw, persistent=False) as rt:
        print(f"{DIM}LLM: {type(rt.context.llm).__name__} · scenario: {scenario}{END}\n")
        thread = f"cli-{uuid.uuid4().hex[:8]}"
        config: RunnableConfig = {"configurable": {"thread_id": thread}, "recursion_limit": 40}
        calls = start_call_log()
        modes: list[StreamMode] = ["custom"]
        streaming = False
        inp = turn_input([HumanMessage(content=message)], "cli-user")
        async for _, raw in rt.graph.astream(inp, config, context=rt.context, stream_mode=modes):
            if not isinstance(raw, dict):
                continue
            ev: dict[str, Any] = raw
            kind = ev.get("event")
            if kind == "token":
                if not streaming:
                    print(f"\n{BOLD}── answer ──{END}")
                    streaming = True
                print(ev["text"], end="", flush=True)
            elif kind == "node" and ev.get("status") == "start":
                print(f"{DIM}▸ {ev['node']}{END}")
            elif kind == "candidates":
                print(f"{DIM}  candidates: {', '.join(t['name'] for t in ev['trails']) or 'none'}{END}")
            elif kind == "verdict":
                color = GREEN if ev["approved"] else RED
                crit = [i["evidence"] for i in ev["issues"] if i["severity"] == "critical"]
                label = "approved" if ev["approved"] else f"VETO ({ev['scope']})"
                print(f"  {color}safety #{ev['attempt']}: {label}{END} {'; '.join(crit)}")
        state = (await rt.graph.aget_state(config)).values
        if not streaming and state.get("messages"):
            print(f"\n{state['messages'][-1].content}")
        print(f"\n\n{DIM}{len(calls)} tool calls: {json.dumps([c['tool'] for c in calls])}{END}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("message")
    ap.add_argument(
        "--scenario",
        default="normal",
        help="normal | kziv_closed | golan_flood_warning | crowded_holiday | everything_closed",
    )
    args = ap.parse_args()
    asyncio.run(run(args.message, args.scenario), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
