from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.types import CollectionResult
from app.db.models import Source
from app.db.repositories import ItemRepository, JobRepository, SettingsRepository, SourceRepository
from app.db.session import SessionFactory
from app.services.integrity import assess_collection_integrity

logger = structlog.get_logger()


@asynccontextmanager
async def keep_job_lease(
    job_id: int,
    worker_id: str,
    lease_seconds: int,
) -> AsyncIterator[None]:
    """Refresh a running job lease while an external API request is in progress."""

    interval = max(5, lease_seconds // 3)
    stopped = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                async with SessionFactory() as session:
                    async with session.begin():
                        owned = await JobRepository.extend_lease(
                            session,
                            job_id=job_id,
                            worker_id=worker_id,
                            lease_seconds=lease_seconds,
                        )
                if not owned:
                    logger.warning("job_lease_lost", job_id=job_id, worker_id=worker_id)
                    return
            except Exception:
                logger.exception("job_lease_heartbeat_failed", job_id=job_id, worker_id=worker_id)

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        stopped.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def persist_collection_result(
    session: AsyncSession,
    source: Source,
    result: CollectionResult,
) -> tuple[int, int]:
    signal_chat_id = await SettingsRepository.get(session, "signal_chat_id", None)

    # Collectors already filter by their scan window. This second boundary is a
    # safety net against old pinned posts/stories or an API returning stale data.
    eligible = []
    for item in result.items:
        if item.published_at is None:
            continue
        if result.window_start and item.published_at <= result.window_start:
            continue
        if result.window_end and item.published_at > result.window_end:
            continue
        eligible.append(item)

    inserted, deliveries = await ItemRepository.ingest(
        session,
        source=source,
        items=eligible,
        signal_chat_id=int(signal_chat_id) if signal_chat_id else None,
    )
    await session.flush()
    integrity = await assess_collection_integrity(session, source, result)
    if integrity.suspected_gap:
        result.needs_immediate_retry = True
        result.diagnostics["integrity_status"] = integrity.status
        result.diagnostics["integrity_details"] = integrity.details
        logger.warning(
            "collection_integrity_gap",
            source_id=source.id,
            platform=source.platform.value,
            details=integrity.details,
        )
    else:
        result.diagnostics["integrity_status"] = "ok"
    post_keys = [item.item_key for item in eligible if item.item_type.value == "post"]
    story_keys = [item.item_key for item in eligible if item.item_type.value == "story"]

    collection_completed = not result.needs_immediate_retry
    was_bootstrap = not source.state or not source.state.bootstrap_completed
    bootstrap_completed = True if was_bootstrap and collection_completed else None
    checkpoint_at = result.window_end if collection_completed else None

    # A committed watermark must never jump across an unfinished VK pagination or
    # a suspected integrity gap. The collector cursor keeps the candidate high-water
    # mark until the missing pages are safely traversed. Telegram incremental batches
    # may advance their committed min_id because each completed batch is contiguous.
    integrity_gap = integrity.suspected_gap
    hold_post_watermark = integrity_gap or (source.platform.value == "vk" and bool(result.post_cursor))
    hold_story_watermark = integrity_gap or bool(result.story_cursor)
    await ItemRepository.update_state(
        session,
        source.id,
        bootstrap_completed=bootstrap_completed,
        checkpoint_at=checkpoint_at,
        post_watermark=None if hold_post_watermark else result.post_watermark,
        story_watermark=None if hold_story_watermark else result.story_watermark,
        post_cursor=result.post_cursor,
        story_cursor=result.story_cursor,
        recent_post_keys=(source.state.recent_post_keys if source.state else []) + post_keys,
        recent_story_keys=(source.state.recent_story_keys if source.state else []) + story_keys,
    )
    published = [item.published_at for item in eligible if item.published_at]
    last_item_at: datetime | None = max(published) if published else None
    await SourceRepository.update_after_success(
        session,
        source.id,
        title=result.title,
        external_id=result.external_id,
        normalized_link=result.normalized_link,
        last_item_at=last_item_at,
        diagnostics=result.diagnostics,
        completed=collection_completed,
    )
    return inserted, deliveries
