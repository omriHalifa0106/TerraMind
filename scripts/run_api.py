"""Start the API with an event loop that psycopg supports on every OS.

psycopg's async mode cannot use Windows' default ProactorEventLoop, so we always run uvicorn
on a SelectorEventLoop (Python 3.12+ `asyncio.run(loop_factory=...)`).
"""

from __future__ import annotations

import argparse
import asyncio

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    server = uvicorn.Server(
        uvicorn.Config("terramind.api.main:app", host=args.host, port=args.port, loop="none")
    )
    asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    main()
