from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import exists, select

from app.db.enums import DeliveryStatus
from app.db.models import Delivery, Item, Media
from app.db.session import SessionFactory


async def cleanup_delivered_media(retention_hours: int, limit: int = 1000) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
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
            deleted = 0
            for media in rows:
                try:
                    Path(media.local_path).unlink(missing_ok=True)
                    media.local_path = ""
                    deleted += 1
                except OSError:
                    continue
            return deleted
