from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.types import CollectionResult
from app.db.models import Source
from app.db.repositories import ItemRepository, SettingsRepository, SourceRepository


async def persist_collection_result(
    session: AsyncSession,
    source: Source,
    result: CollectionResult,
) -> tuple[int, int]:
    signal_chat_id = await SettingsRepository.get(session, "signal_chat_id", None)
    bootstrap = not source.state or not source.state.bootstrap_completed
    inserted, deliveries = await ItemRepository.ingest(
        session,
        source=source,
        items=result.items,
        bootstrap=bootstrap,
        signal_chat_id=int(signal_chat_id) if signal_chat_id else None,
    )
    post_keys = [item.item_key for item in result.items if item.item_type.value == "post"]
    story_keys = [item.item_key for item in result.items if item.item_type.value == "story"]
    bootstrap_completed = not result.needs_immediate_retry
    await ItemRepository.update_state(
        session,
        source.id,
        bootstrap_completed=bootstrap_completed if bootstrap else None,
        post_watermark=result.post_watermark,
        story_watermark=result.story_watermark,
        post_cursor=result.post_cursor,
        story_cursor=result.story_cursor,
        recent_post_keys=(source.state.recent_post_keys if source.state else []) + post_keys,
        recent_story_keys=(source.state.recent_story_keys if source.state else []) + story_keys,
    )
    published = [item.published_at for item in result.items if item.published_at]
    last_item_at: datetime | None = max(published) if published else None
    await SourceRepository.update_after_success(
        session,
        source.id,
        title=result.title,
        external_id=result.external_id,
        normalized_link=result.normalized_link,
        last_item_at=last_item_at,
        diagnostics=result.diagnostics,
    )
    return inserted, deliveries
