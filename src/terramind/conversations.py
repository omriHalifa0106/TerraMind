"""Conversation memory: the previous turn of each chat, in the `conversations` table.

The API loads it before a graph run and saves the new state after it. Agents only see it as graph
input (`previous`), so they still never touch the database. A database problem never fails a chat:
the message is then simply handled as a new trip.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg
from pydantic import ValidationError

from terramind.schemas import ChatLine, PreviousTurn
from terramind.schemas.conversation import HISTORY_LINES

log = logging.getLogger("terramind.conversations")

THREAD_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")  # what the browser generates (a UUID)
DB_ERRORS = (asyncpg.PostgresError, OSError, TimeoutError)

UPSERT = """
INSERT INTO conversations (thread_id, state) VALUES ($1, $2::jsonb)
ON CONFLICT (thread_id) DO UPDATE SET
    state = EXCLUDED.state, turns = conversations.turns + 1, updated_at = now()
"""


class ConversationStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def open(cls, dsn: str) -> ConversationStore:
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=4, timeout=10, command_timeout=5))

    async def close(self) -> None:
        await self._pool.close()

    async def load(self, thread_id: str) -> PreviousTurn | None:
        if not THREAD_ID.match(thread_id):
            return None
        try:
            raw = await self._pool.fetchval("SELECT state FROM conversations WHERE thread_id = $1", thread_id)
        except DB_ERRORS as exc:
            log.warning(
                "conversation load failed, treating the message as a new trip: %s", type(exc).__name__
            )
            return None
        if raw is None:
            return None
        try:
            return PreviousTurn.model_validate_json(raw)
        except ValidationError:
            log.warning("stored conversation %s no longer matches the schema; starting over", thread_id)
            return None

    async def save(self, thread_id: str, turn: PreviousTurn) -> None:
        if not THREAD_ID.match(thread_id):
            return
        try:
            await self._pool.execute(UPSERT, thread_id, turn.model_dump_json())
        except DB_ERRORS as exc:
            log.warning("conversation save failed: %s", type(exc).__name__)


def next_turn(
    final_state: dict[str, Any], previous: PreviousTurn | None, user_text: str, answer: str
) -> PreviousTurn | None:
    """What to remember after a run: its request, search results, checks and plan, plus the chat lines.
    Nothing is remembered when agent 1 could not understand the request (no region yet)."""
    request = final_state.get("request")
    if request is None:
        return previous
    history = [*(previous.history if previous else []), ChatLine(role="user", text=user_text[:4000])]
    if answer:
        history.append(ChatLine(role="assistant", text=answer[:4000]))
    return PreviousTurn(
        request=request,
        candidates=final_state.get("candidates", []),
        assessments=final_state.get("assessments", []),
        evidence=final_state.get("evidence", []),
        plan=final_state.get("plan"),
        plan_rationale=final_state.get("plan_rationale"),
        excluded_trail_ids=final_state.get("excluded_trail_ids", []),
        history=history[-HISTORY_LINES:],
    )
