"""Test setup. Unit tests need nothing; tests marked `db` need TERRAMIND_TEST_DATABASE_URL.

export TERRAMIND_TEST_DATABASE_URL=postgresql://terramind:terramind@localhost:5433/terramind_test
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TERRAMIND_TEST_DATABASE_URL")
# Configure before any terramind import reads settings.
os.environ["EMBEDDING_BACKEND"] = "hash"
os.environ["HTTP_TIMEOUT_S"] = "1"
os.environ["ANTHROPIC_AUTH_TOKEN"] = ""  # tests inject tests.fakes.FakeLLM, never the real API
os.environ["LANGSMITH_TRACING"] = "false"  # tests never send traces, even with a real key in .env
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

FIXTURE = Path(__file__).parent / "fixtures" / "overpass_sample.json"
PLACES_FIXTURE = Path(__file__).parent / "fixtures" / "places_sample.json"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DB:
        return
    skip = pytest.mark.skip(reason="set TERRAMIND_TEST_DATABASE_URL to run database tests")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
async def loaded_db() -> AsyncIterator[str]:
    """Migrate and load the synthetic fixture once per session (idempotent)."""
    assert TEST_DB
    import asyncpg

    from ingestion import derive_attributes, osm_loader, places_loader, seed_knowledge
    from scripts.migrate import migrate

    await migrate(TEST_DB)
    await osm_loader.run(FIXTURE, refresh=False, dsn=TEST_DB)
    await derive_attributes.run(TEST_DB)
    await seed_knowledge.run(seed_knowledge.DEFAULT_FILE, TEST_DB)
    await places_loader.run(PLACES_FIXTURE, dsn=TEST_DB)
    yield TEST_DB
    from mcp_servers.common.db import close_pool

    await close_pool()
    conn = await asyncpg.connect(TEST_DB)
    await conn.close()
