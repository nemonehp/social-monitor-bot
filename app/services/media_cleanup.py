from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import exists, select

from app.db.enums import DeliveryStatus
from app.db.models import Delivery, Item, Media
from app.db.session import SessionFactory


def _delete_files(paths: list[str], *, remove_empty_parents: bool) -> set[str]:
    deleted: set[str] = set()
    parents: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        deleted.add(raw_path)
        if remove_empty_parents:
            parents.add(path.parent)
    for parent in sorted(parents, key=lambda item: len(item.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            continue
    return deleted


async def cleanup_delivered_media(retention_hours: int, limit: int = 1000) -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    async with SessionFactory() as session:
        async with session.begin():
            rows = list(
                await session.scalars(
                    select(Media)
                    .join(Item, Item.id == Media.item_id)
                    .where(
                        Media.local_path != "",
                        Media.created_at < cutoff,
                        ~exists(
                            select(Delivery.id).where(
                                Delivery.item_id == Media.item_id,
                                Delivery.status.in_([
                                    DeliveryStatus.PENDING,
                                    DeliveryStatus.RUNNING,
                                    DeliveryStatus.RETRY,
                                ]),
                            )
                        ),
                    )
                    .limit(limit)
                )
            )
            deleted_paths = await asyncio.to_thread(
                _delete_files,
                [media.local_path for media in rows],
                remove_empty_parents=False,
            )
            for media in rows:
                if media.local_path in deleted_paths:
                    media.local_path = ""
            return len(deleted_paths)


async def cleanup_item_media(item_id: int) -> int:
    """Delete local previews as soon as every delivery for the item is complete."""
    async with SessionFactory() as session:
        async with session.begin():
            outstanding = await session.scalar(
                select(
                    exists().where(
                        Delivery.item_id == item_id,
                        Delivery.status.in_([
                            DeliveryStatus.PENDING,
                            DeliveryStatus.RUNNING,
                            DeliveryStatus.RETRY,
                        ]),
                    )
                )
            )
            if outstanding:
                return 0
            rows = list(
                await session.scalars(
                    select(Media).where(Media.item_id == item_id, Media.local_path != "")
                )
            )
            deleted_paths = await asyncio.to_thread(
                _delete_files,
                [media.local_path for media in rows],
                remove_empty_parents=True,
            )
            for media in rows:
                if media.local_path in deleted_paths:
                    media.local_path = ""
            return len(deleted_paths)
