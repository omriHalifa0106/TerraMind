"""Start the API (FastAPI + uvicorn) on http://127.0.0.1:8000."""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("terramind.api.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
