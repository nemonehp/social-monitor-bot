from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.types import CollectionResult
from app.db.enums import ItemType
from app.db.models import IntegrityCheck, Item, Source


@dataclass(frozen=True, slots=True)
class IntegrityAssessment:
    status: str
    suspected_gap: bool
    details: dict[str, object]


def _as_int(value: object) -> int:
    try:
        return int(str(value or "0").rsplit("_", 1)[-1])
    except ValueError:
        return 0


def _next_consecutive_gap(value: object) -> int:
    """Increment an ORM counter safely before SQLAlchemy insert defaults are applied."""

    try:
        return max(0, int(value or 0)) + 1
    except (TypeError, ValueError):
        return 1


def _item_ids(external_id: str, raw_json: dict[str, Any] | None) -> set[int]:
    ids = {_as_int(external_id)}
    raw = raw_json or {}
    for value in raw.get("message_ids", []) or []:
        ids.add(_as_int(value))
    representative = raw.get("representative") or {}
    if isinstance(representative, dict):
        ids.add(_as_int(representative.get("id")))
    story = raw.get("story") or {}
    if isinstance(story, dict):
        ids.add(_as_int(story.get("id")))
    ids.discard(0)
    return ids


def _result_contains(result: CollectionResult, item_type: ItemType, remote_id: int) -> bool:
    if remote_id <= 0:
        return True
    return any(
        item.item_type == item_type and remote_id in _item_ids(item.external_id, item.raw)
        for item in result.items
    )


async def _stored_ids(
    session: AsyncSession,
    source_id: int,
    item_type: ItemType,
    *,
    limit: int = 500,
) -> set[int]:
    rows = await session.execute(
        select(Item.external_id, Item.raw_json)
        .where(Item.source_id == source_id, Item.item_type == item_type)
        .order_by(Item.published_at.desc(), Item.id.desc())
        .limit(limit)
    )
    result: set[int] = set()
    for external_id, raw_json in rows:
        result.update(_item_ids(str(external_id or ""), raw_json if isinstance(raw_json, dict) else {}))
    return result


async def assess_collection_integrity(
    session: AsyncSession,
    source: Source,
    result: CollectionResult,
) -> IntegrityAssessment:
    remote_post = _as_int(result.diagnostics.get("remote_latest_post_id") or result.post_watermark)
    remote_story = _as_int(result.diagnostics.get("remote_latest_story_id") or result.story_watermark)
    post_ids = await _stored_ids(session, source.id, ItemType.POST)
    story_ids = await _stored_ids(session, source.id, ItemType.STORY)
    stored_post = max(post_ids, default=0)
    stored_story = max(story_ids, default=0)
    state_post = _as_int(source.state.post_watermark if source.state else 0)
    state_story = _as_int(source.state.story_watermark if source.state else 0)

    # The first pass intentionally establishes a baseline without importing old
    # history. Comparing that baseline with an empty database would be a false gap.
    first_run = bool(result.diagnostics.get("first_run"))
    post_gap = (
        not first_run
        and remote_post > max(state_post, stored_post)
        and remote_post not in post_ids
        and not _result_contains(result, ItemType.POST, remote_post)
    )
    story_gap = (
        not first_run
        and remote_story > max(state_story, stored_story)
        and remote_story not in story_ids
        and not _result_contains(result, ItemType.STORY, remote_story)
    )
    suspected = post_gap or story_gap
    details: dict[str, object] = {
        "first_run": first_run,
        "remote_post_id": remote_post,
        "stored_post_id": stored_post,
        "state_post_id": state_post,
        "remote_story_id": remote_story,
        "stored_story_id": stored_story,
        "state_story_id": state_story,
        "post_gap": post_gap,
        "story_gap": story_gap,
    }
    row = await session.get(IntegrityCheck, source.id)
    if row is None:
        # SQLAlchemy applies mapped_column(default=0) during INSERT, not when the
        # Python object is constructed. Set the counter explicitly so the first
        # suspected gap cannot evaluate None + 1 before session.flush().
        row = IntegrityCheck(source_id=source.id, consecutive_gaps=0)
        session.add(row)
    row.last_checked_at = datetime.now(UTC)
    row.last_remote_post_id = str(remote_post or "")
    row.last_remote_story_id = str(remote_story or "")
    row.last_stored_post_id = str(stored_post or "")
    row.last_stored_story_id = str(stored_story or "")
    row.details_json = details
    if suspected:
        row.status = "suspected_gap"
        row.consecutive_gaps = _next_consecutive_gap(row.consecutive_gaps)
    else:
        row.status = "ok"
        row.consecutive_gaps = 0
    return IntegrityAssessment(row.status, suspected, details)
