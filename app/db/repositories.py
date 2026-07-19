from __future__ import annotations

import builtins
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from typing import cast as type_cast

from sqlalchemy import String, and_, case, cast, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.collectors.types import CollectedItem
from app.db.enums import (
    CredentialPlatform,
    CredentialStatus,
    DeliveryStatus,
    JobStatus,
    Platform,
    ProxyStatus,
    SourceStatus,
)
from app.db.models import (
    AlertState,
    AllowedUser,
    AppSetting,
    AuditLog,
    CollectionJob,
    Credential,
    Delivery,
    Item,
    Media,
    Proxy,
    Source,
    SourceState,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _rowcount(result: Any) -> int:
    return int(type_cast(CursorResult[Any], result).rowcount or 0)


class SettingsRepository:
    @staticmethod
    async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
        row = await session.get(AppSetting, key)
        return default if row is None else row.value.get("value", default)

    @staticmethod
    async def set(session: AsyncSession, key: str, value: Any) -> None:
        stmt = insert(AppSetting).values(key=key, value={"value": value})
        stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": {"value": value}})
        await session.execute(stmt)


class AccessRepository:
    @staticmethod
    async def ensure_admin(session: AsyncSession, admin_id: int) -> None:
        stmt = insert(AllowedUser).values(
            telegram_id=admin_id,
            display_name="Администратор",
            added_by=admin_id,
            active=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AllowedUser.telegram_id], set_={"active": True, "added_by": admin_id}
        )
        await session.execute(stmt)

    @staticmethod
    async def is_allowed(session: AsyncSession, telegram_id: int, admin_id: int) -> bool:
        if telegram_id == admin_id:
            return True
        value = await session.scalar(
            select(AllowedUser.active).where(AllowedUser.telegram_id == telegram_id)
        )
        return bool(value)

    @staticmethod
    async def add_user(session: AsyncSession, telegram_id: int, name: str, admin_id: int) -> None:
        stmt = insert(AllowedUser).values(
            telegram_id=telegram_id,
            display_name=name,
            added_by=admin_id,
            active=True,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AllowedUser.telegram_id],
            set_={"display_name": name, "active": True, "added_by": admin_id},
        )
        await session.execute(stmt)

    @staticmethod
    async def disable_user(session: AsyncSession, telegram_id: int) -> None:
        await session.execute(
            update(AllowedUser).where(AllowedUser.telegram_id == telegram_id).values(active=False)
        )

    @staticmethod
    async def list_users(session: AsyncSession) -> list[AllowedUser]:
        result = await session.scalars(select(AllowedUser).order_by(AllowedUser.telegram_id))
        return list(result)


class AuditRepository:
    @staticmethod
    async def write(
        session: AsyncSession,
        actor_id: int,
        action: str,
        entity_type: str = "",
        entity_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditLog(
                actor_telegram_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload or {},
            )
        )


class SourceRepository:
    @staticmethod
    async def add(
        session: AsyncSession,
        *,
        platform: Platform,
        input_link: str,
        normalized_link: str,
        region: str,
        federal_district: str,
        added_by: int,
        category: str | None = None,
        subcategory: str | None = None,
        title: str = "",
        external_id: str = "",
        metadata: dict[str, Any] | None = None,
        next_check_at: datetime | None = None,
    ) -> tuple[Source, bool]:
        category = federal_district if category is None else category
        subcategory = region if subcategory is None else subcategory
        existing = await session.scalar(
            select(Source).where(
                Source.platform == platform,
                Source.normalized_link == normalized_link,
            )
        )
        if existing:
            if existing.status == SourceStatus.DELETED:
                existing.status = SourceStatus.ACTIVE
            if region:
                existing.region = region
            if federal_district:
                existing.federal_district = federal_district
            if category:
                existing.category = category
            if subcategory:
                existing.subcategory = subcategory
            if title and not existing.title:
                existing.title = title
            return existing, False

        started_at = utcnow()
        source = Source(
            platform=platform,
            input_link=input_link,
            normalized_link=normalized_link,
            region=region,
            federal_district=federal_district,
            category=category,
            subcategory=subcategory,
            added_by=added_by,
            title=title,
            external_id=external_id,
            metadata_json=metadata or {},
            next_check_at=next_check_at or started_at,
        )
        source.state = SourceState(monitor_from_at=started_at, checkpoint_at=started_at)
        session.add(source)
        await session.flush()
        return source, True

    @staticmethod
    async def bulk_add(
        session: AsyncSession,
        rows: list[dict[str, Any]],
        *,
        added_by: int,
    ) -> tuple[int, int]:
        if not rows:
            return 0, 0
        existing_map: dict[tuple[Platform, str], Source] = {}
        for start in range(0, len(rows), 5000):
            chunk = rows[start : start + 5000]
            keys = [(Platform(row["platform"]), row["normalized_link"]) for row in chunk]
            existing_rows = await session.scalars(
                select(Source).where(tuple_(Source.platform, Source.normalized_link).in_(keys))
            )
            existing_map.update({(row.platform, row.normalized_link): row for row in existing_rows})
        created = updated = 0
        for row in rows:
            platform = Platform(row["platform"])
            key = (platform, row["normalized_link"])
            existing_source = existing_map.get(key)
            if existing_source:
                if existing_source.status == SourceStatus.DELETED:
                    existing_source.status = SourceStatus.ACTIVE
                if row.get("region"):
                    existing_source.region = row["region"]
                if row.get("federal_district"):
                    existing_source.federal_district = row["federal_district"]
                category = row.get("category") or row.get("federal_district") or ""
                subcategory = row.get("subcategory") or row.get("region") or ""
                if category:
                    existing_source.category = category
                if subcategory:
                    existing_source.subcategory = subcategory
                if row.get("title"):
                    existing_source.title = row["title"][:500]
                if row.get("external_id"):
                    existing_source.external_id = str(row["external_id"])[:255]
                updated += 1
                continue
            started_at = utcnow()
            source = Source(
                platform=platform,
                input_link=row["input_link"],
                normalized_link=row["normalized_link"],
                region=row.get("region", ""),
                federal_district=row.get("federal_district", ""),
                category=row.get("category") or row.get("federal_district", ""),
                subcategory=row.get("subcategory") or row.get("region", ""),
                added_by=added_by,
                title=row.get("title", ""),
                external_id=str(row.get("external_id", "")),
                metadata_json=row.get("metadata", {}),
                next_check_at=row.get("next_check_at") or started_at,
            )
            source.state = SourceState(monitor_from_at=started_at, checkpoint_at=started_at)
            session.add(source)
            existing_map[key] = source
            created += 1
        await session.flush()
        return created, updated

    @staticmethod
    async def get(session: AsyncSession, source_id: int) -> Source | None:
        return type_cast(
            Source | None,
            await session.scalar(
                select(Source)
                .where(Source.id == source_id)
                .options(selectinload(Source.state))
            ),
        )

    @staticmethod
    async def list(
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = 8,
        query: str = "",
        platform: Platform | None = None,
        status: SourceStatus | None = None,
        region: str | None = None,
        federal_district: str | None = None,
        category: str | None = None,
        subcategory: str | None = None,
        alphabetical: bool = False,
    ) -> tuple[list[Source], int]:
        filters = [Source.status != SourceStatus.DELETED]
        if query:
            token = f"%{query.strip()}%"
            filters.append(
                or_(
                    Source.title.ilike(token),
                    Source.input_link.ilike(token),
                    Source.normalized_link.ilike(token),
                    Source.external_id.ilike(token),
                    Source.region.ilike(token),
                    Source.category.ilike(token),
                    Source.subcategory.ilike(token),
                    cast(Source.id, String).ilike(token),
                )
            )
        if platform:
            filters.append(Source.platform == platform)
        if status:
            filters.append(Source.status == status)
        if region is not None:
            filters.append(Source.region == region)
        if federal_district is not None:
            filters.append(Source.federal_district == federal_district)
        if category is not None:
            filters.append(Source.category == category)
        if subcategory is not None:
            filters.append(Source.subcategory == subcategory)
        total = int(await session.scalar(select(func.count()).select_from(Source).where(*filters)) or 0)
        alphabetical_key = func.lower(
            func.coalesce(func.nullif(Source.title, ""), Source.normalized_link)
        )
        order_by: tuple[Any, ...]
        if query:
            clean_query = query.strip()
            order_by = (
                case(
                    (cast(Source.id, String) == clean_query, 0),
                    (func.lower(Source.external_id) == clean_query.lower(), 1),
                    else_=2,
                ),
                alphabetical_key,
                Source.id,
            )
        elif alphabetical:
            order_by = (alphabetical_key, Source.id)
        else:
            order_by = (Source.category, Source.subcategory, Source.title, Source.id)
        rows = await session.scalars(
            select(Source)
            .where(*filters)
            .order_by(*order_by)
            .offset(max(0, page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total

    @staticmethod
    async def category_counts(session: AsyncSession) -> builtins.list[tuple[str, int]]:
        rows = await session.execute(
            select(Source.category, func.count())
            .where(Source.status != SourceStatus.DELETED)
            .group_by(Source.category)
            .order_by(func.lower(Source.category))
        )
        return [(category or "", int(count)) for category, count in rows]

    @staticmethod
    async def subcategory_counts(
        session: AsyncSession,
        category: str,
    ) -> builtins.list[tuple[str, int]]:
        rows = await session.execute(
            select(Source.subcategory, func.count())
            .where(
                Source.status != SourceStatus.DELETED,
                Source.category == category,
            )
            .group_by(Source.subcategory)
            .order_by(func.lower(Source.subcategory))
        )
        return [(subcategory or "", int(count)) for subcategory, count in rows]

    @staticmethod
    async def district_counts(session: AsyncSession) -> builtins.list[tuple[str, int]]:
        rows = await session.execute(
            select(Source.federal_district, func.count())
            .where(Source.status != SourceStatus.DELETED)
            .group_by(Source.federal_district)
            .order_by(func.lower(Source.federal_district))
        )
        return [(district or "", int(count)) for district, count in rows]

    @staticmethod
    async def region_counts(
        session: AsyncSession,
        federal_district: str,
    ) -> builtins.list[tuple[str, int]]:
        rows = await session.execute(
            select(Source.region, func.count())
            .where(
                Source.status != SourceStatus.DELETED,
                Source.federal_district == federal_district,
            )
            .group_by(Source.region)
            .order_by(func.lower(Source.region))
        )
        return [(region or "", int(count)) for region, count in rows]

    @staticmethod
    async def set_status(session: AsyncSession, source_id: int, status: SourceStatus) -> None:
        await session.execute(update(Source).where(Source.id == source_id).values(status=status))

    @staticmethod
    async def update_region(
        session: AsyncSession, source_id: int, region: str, federal_district: str
    ) -> None:
        await session.execute(
            update(Source)
            .where(Source.id == source_id)
            .values(
                region=region,
                federal_district=federal_district,
                category=federal_district,
                subcategory=region,
            )
        )

    @staticmethod
    async def update_category(
        session: AsyncSession,
        source_id: int,
        category: str,
        subcategory: str,
        *,
        sync_location: bool = False,
    ) -> None:
        values: dict[str, Any] = {"category": category, "subcategory": subcategory}
        if sync_location:
            values.update(federal_district=category, region=subcategory)
        await session.execute(update(Source).where(Source.id == source_id).values(**values))

    @staticmethod
    async def update_after_success(
        session: AsyncSession,
        source_id: int,
        *,
        title: str = "",
        external_id: str = "",
        normalized_link: str = "",
        last_item_at: datetime | None = None,
        diagnostics: dict[str, Any] | None = None,
        completed: bool = True,
    ) -> None:
        source = await session.get(Source, source_id)
        if not source:
            return
        source.last_check_at = utcnow()
        if completed:
            source.last_success_at = utcnow()
        if completed:
            source.consecutive_failures = 0
            source.last_error_code = ""
            source.last_error_text = ""
        if title:
            source.title = title[:500]
        if external_id:
            source.external_id = external_id[:255]
        if normalized_link:
            source.normalized_link = normalized_link
        if last_item_at and (not source.last_item_at or last_item_at > source.last_item_at):
            source.last_item_at = last_item_at
        if diagnostics:
            source.metadata_json = {**(source.metadata_json or {}), "last_diagnostics": diagnostics}

    @staticmethod
    async def update_after_error(
        session: AsyncSession, source_id: int, code: str, text: str, terminal: bool = False
    ) -> None:
        source = await session.get(Source, source_id)
        if not source:
            return
        source.last_check_at = utcnow()
        source.consecutive_failures += 1
        source.last_error_code = code[:100]
        source.last_error_text = text[:4000]
        if terminal:
            source.status = SourceStatus.ERROR


class JobRepository:
    @staticmethod
    async def schedule_due_sources(
        session: AsyncSession,
        *,
        default_interval: int,
        limit: int = 1000,
    ) -> int:
        now = utcnow()
        sources = list(
            await session.scalars(
                select(Source)
                .where(Source.status == SourceStatus.ACTIVE, Source.next_check_at <= now)
                .order_by(Source.next_check_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        created = 0
        source_ids = [source.id for source in sources]
        active_source_ids: set[int] = set()
        if source_ids:
            active_source_ids = set(
                await session.scalars(
                    select(CollectionJob.source_id).where(
                        CollectionJob.source_id.in_(source_ids),
                        CollectionJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING, JobStatus.RETRY]),
                    )
                )
            )
        for source in sources:
            interval = source.poll_interval_seconds or default_interval
            source.next_check_at = now + timedelta(seconds=interval)
            if source.id in active_source_ids:
                continue
            session.add(
                CollectionJob(
                    source_id=source.id,
                    platform=source.platform,
                    status=JobStatus.PENDING,
                    run_after=now,
                )
            )
            created += 1
        return created

    @staticmethod
    async def recover_expired(session: AsyncSession) -> int:
        result = await session.execute(
            update(CollectionJob)
            .where(
                CollectionJob.status == JobStatus.RUNNING,
                CollectionJob.locked_until < utcnow(),
            )
            .values(status=JobStatus.RETRY, run_after=utcnow(), worker_id="", locked_until=None)
        )
        return _rowcount(result)

    @staticmethod
    async def claim(
        session: AsyncSession,
        *,
        platform: Platform,
        worker_id: str,
        lease_seconds: int,
    ) -> CollectionJob | None:
        now = utcnow()
        job = await session.scalar(
            select(CollectionJob)
            .where(
                CollectionJob.platform == platform,
                CollectionJob.status.in_([JobStatus.PENDING, JobStatus.RETRY]),
                CollectionJob.run_after <= now,
            )
            .order_by(CollectionJob.run_after, CollectionJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = JobStatus.RUNNING
        job.worker_id = worker_id
        job.locked_until = now + timedelta(seconds=lease_seconds)
        job.attempts += 1
        await session.flush()
        return job

    @staticmethod
    async def complete(session: AsyncSession, job_id: int) -> None:
        await session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(status=JobStatus.DONE, worker_id="", locked_until=None, last_error="")
        )

    @staticmethod
    async def extend_lease(
        session: AsyncSession,
        job_id: int,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await session.execute(
            update(CollectionJob)
            .where(
                CollectionJob.id == job_id,
                CollectionJob.status == JobStatus.RUNNING,
                CollectionJob.worker_id == worker_id,
            )
            .values(locked_until=utcnow() + timedelta(seconds=lease_seconds))
        )
        return _rowcount(result) > 0

    @staticmethod
    async def retry(
        session: AsyncSession,
        job_id: int,
        error: str,
        *,
        delay_seconds: int,
        final: bool = False,
    ) -> None:
        await session.execute(
            update(CollectionJob)
            .where(CollectionJob.id == job_id)
            .values(
                status=JobStatus.FAILED if final else JobStatus.RETRY,
                run_after=utcnow() + timedelta(seconds=delay_seconds),
                worker_id="",
                locked_until=None,
                last_error=error[:4000],
            )
        )


class ItemRepository:
    @staticmethod
    async def known_keys(session: AsyncSession, source_id: int, keys: Iterable[str]) -> set[str]:
        keys = list(keys)
        if not keys:
            return set()
        rows = await session.scalars(
            select(Item.item_key).where(Item.source_id == source_id, Item.item_key.in_(keys))
        )
        return set(rows)

    @staticmethod
    async def ingest(
        session: AsyncSession,
        *,
        source: Source,
        items: list[CollectedItem],
        signal_chat_id: int | None,
    ) -> tuple[int, int]:
        inserted = 0
        deliveries = 0
        ordered_items = sorted(
            items,
            key=lambda payload: (payload.published_at or utcnow(), payload.item_key),
        )
        for payload in ordered_items:
            stmt = (
                insert(Item)
                .values(
                    source_id=source.id,
                    platform=payload.platform,
                    item_type=payload.item_type,
                    item_key=payload.item_key,
                    external_id=payload.external_id,
                    original_url=payload.original_url,
                    text=payload.text,
                    published_at=payload.published_at,
                    is_pinned=payload.is_pinned,
                    raw_json=payload.raw,
                )
                .on_conflict_do_nothing(index_elements=[Item.item_key])
                .returning(Item.id)
            )
            item_id = await session.scalar(stmt)
            if not item_id:
                continue
            inserted += 1
            for position, media_payload in enumerate(payload.media):
                session.add(
                    Media(
                        item_id=item_id,
                        position=position,
                        media_type=media_payload.media_type,
                        remote_url=media_payload.remote_url,
                        preview_url=media_payload.preview_url,
                        local_path=media_payload.local_path,
                        mime_type=media_payload.mime_type,
                        width=media_payload.width,
                        height=media_payload.height,
                        duration=media_payload.duration,
                        metadata_json=media_payload.metadata,
                    )
                )
            if signal_chat_id:
                session.add(
                    Delivery(
                        item_id=item_id,
                        target_chat_id=signal_chat_id,
                        status=DeliveryStatus.PENDING,
                        run_after=utcnow(),
                    )
                )
                deliveries += 1
        return inserted, deliveries

    @staticmethod
    async def update_state(
        session: AsyncSession,
        source_id: int,
        *,
        bootstrap_completed: bool | None = None,
        post_watermark: str | None = None,
        story_watermark: str | None = None,
        post_cursor: dict[str, Any] | None = None,
        story_cursor: dict[str, Any] | None = None,
        recent_post_keys: list[str] | None = None,
        recent_story_keys: list[str] | None = None,
        monitor_from_at: datetime | None = None,
        checkpoint_at: datetime | None = None,
    ) -> None:
        state = await session.get(SourceState, source_id)
        if not state:
            state = SourceState(source_id=source_id)
            session.add(state)
        if bootstrap_completed is not None:
            state.bootstrap_completed = bootstrap_completed
        if monitor_from_at is not None:
            state.monitor_from_at = monitor_from_at
        if checkpoint_at is not None:
            state.checkpoint_at = checkpoint_at
        if post_watermark is not None:
            state.post_watermark = post_watermark
        if story_watermark is not None:
            state.story_watermark = story_watermark
        if post_cursor is not None:
            state.post_cursor = post_cursor
        if story_cursor is not None:
            state.story_cursor = story_cursor
        if recent_post_keys is not None:
            state.recent_post_keys = recent_post_keys[-500:]
        if recent_story_keys is not None:
            state.recent_story_keys = recent_story_keys[-500:]


class DeliveryRepository:
    @staticmethod
    async def recover_expired(session: AsyncSession) -> int:
        result = await session.execute(
            update(Delivery)
            .where(Delivery.status == DeliveryStatus.RUNNING, Delivery.locked_until < utcnow())
            .values(status=DeliveryStatus.RETRY, run_after=utcnow(), locked_until=None)
        )
        return _rowcount(result)

    @staticmethod
    def _load_options():
        return (
            selectinload(Delivery.item).selectinload(Item.source),
            selectinload(Delivery.item).selectinload(Item.media),
        )

    @staticmethod
    async def claim(session: AsyncSession, lease_seconds: int) -> Delivery | None:
        batch = await DeliveryRepository.claim_batch(session, lease_seconds=lease_seconds, batch_size=1)
        return batch[0] if batch else None

    @staticmethod
    async def claim_batch(
        session: AsyncSession,
        *,
        lease_seconds: int,
        batch_size: int,
    ) -> list[Delivery]:
        """Claim one source batch and keep its publications in source chronology.

        The source whose oldest due publication is earliest wins the next batch.
        All currently due publications from that source/chat are then claimed and
        delivered sequentially, preventing interleaving between communities.
        """
        now = utcnow()
        first = await session.scalar(
            select(Delivery)
            .join(Delivery.item)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRY]),
                Delivery.run_after <= now,
            )
            .order_by(Item.published_at.asc().nulls_last(), Item.id, Delivery.id)
            .with_for_update(skip_locked=True)
            .options(*DeliveryRepository._load_options())
            .limit(1)
        )
        if not first:
            return []
        source_id = first.item.source_id
        target_chat_id = first.target_chat_id
        batch_query = (
            select(Delivery)
            .join(Delivery.item)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRY]),
                Delivery.run_after <= now,
                Delivery.target_chat_id == target_chat_id,
                Item.source_id == source_id,
            )
            .order_by(Item.published_at.asc().nulls_last(), Item.id, Delivery.id)
            .with_for_update(skip_locked=True)
            .options(*DeliveryRepository._load_options())
        )
        if batch_size > 0:
            batch_query = batch_query.limit(batch_size)
        rows = list(await session.scalars(batch_query))
        locked_until = now + timedelta(seconds=lease_seconds)
        for delivery in rows:
            delivery.status = DeliveryStatus.RUNNING
            delivery.locked_until = locked_until
        await session.flush()
        return rows

    @staticmethod
    async def start_attempt(session: AsyncSession, delivery_id: int) -> int | None:
        return await session.scalar(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == DeliveryStatus.RUNNING,
            )
            .values(attempts=Delivery.attempts + 1)
            .returning(Delivery.attempts)
        )

    @staticmethod
    async def release(session: AsyncSession, delivery_ids: list[int], delay_seconds: int = 0) -> None:
        if not delivery_ids:
            return
        await session.execute(
            update(Delivery)
            .where(Delivery.id.in_(delivery_ids), Delivery.status == DeliveryStatus.RUNNING)
            .values(
                status=DeliveryStatus.PENDING,
                run_after=utcnow() + timedelta(seconds=max(0, delay_seconds)),
                locked_until=None,
            )
        )

    @staticmethod
    async def extend_leases(
        session: AsyncSession,
        delivery_ids: list[int],
        lease_seconds: int,
    ) -> int:
        if not delivery_ids:
            return 0
        result = await session.execute(
            update(Delivery)
            .where(
                Delivery.id.in_(delivery_ids),
                Delivery.status == DeliveryStatus.RUNNING,
            )
            .values(locked_until=utcnow() + timedelta(seconds=lease_seconds))
        )
        return _rowcount(result)

    @staticmethod
    async def sent(session: AsyncSession, delivery_id: int, message_ids: list[int]) -> None:
        await session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status=DeliveryStatus.SENT,
                telegram_message_ids=message_ids,
                locked_until=None,
                last_error="",
            )
        )

    @staticmethod
    async def retry(
        session: AsyncSession,
        delivery_id: int,
        error: str,
        delay_seconds: int,
        final: bool = False,
    ) -> None:
        await session.execute(
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status=DeliveryStatus.FAILED if final else DeliveryStatus.RETRY,
                run_after=utcnow() + timedelta(seconds=delay_seconds),
                locked_until=None,
                last_error=error[:4000],
            )
        )


class CredentialRepository:
    @staticmethod
    async def add(
        session: AsyncSession,
        platform: CredentialPlatform,
        label: str,
        encrypted_secret: str,
        config: dict[str, Any],
    ) -> tuple[Credential, bool]:
        existing = await session.scalar(
            select(Credential).where(Credential.platform == platform, Credential.label == label)
        )
        if existing:
            existing.secret_encrypted = encrypted_secret
            existing.config_json = config
            existing.status = CredentialStatus.ACTIVE
            existing.cooldown_until = None
            existing.last_error = ""
            existing.health_failures = 0
            existing.dead_since = None
            existing.dead_notified_at = None
            return existing, False
        row = Credential(
            platform=platform,
            label=label,
            secret_encrypted=encrypted_secret,
            config_json=config,
            status=CredentialStatus.ACTIVE,
        )
        session.add(row)
        await session.flush()
        return row, True

    @staticmethod
    async def available(
        session: AsyncSession, platform: CredentialPlatform, limit: int = 100
    ) -> list[Credential]:
        now = utcnow()
        rows = await session.scalars(
            select(Credential)
            .where(
                Credential.platform == platform,
                or_(
                    Credential.status == CredentialStatus.ACTIVE,
                    and_(
                        Credential.status.in_([CredentialStatus.COOLDOWN, CredentialStatus.LIMITED]),
                        Credential.cooldown_until <= now,
                    ),
                ),
            )
            .order_by(Credential.requests_count, Credential.last_success_at.nullsfirst())
            .limit(limit)
        )
        return list(rows)

    @staticmethod
    async def mark_success(
        session: AsyncSession,
        credential_id: int,
        *,
        health_verified: bool = True,
    ) -> None:
        now = utcnow()
        values: dict[str, Any] = {
            "status": CredentialStatus.ACTIVE,
            "cooldown_until": None,
            "last_success_at": now,
            "last_error": "",
            "requests_count": Credential.requests_count + 1,
            "last_health_check_at": now,
            "dead_since": None,
            "dead_notified_at": None,
        }
        if health_verified:
            values.update(last_health_ok_at=now, health_failures=0)
        else:
            values["health_failures"] = Credential.health_failures + 1
        await session.execute(update(Credential).where(Credential.id == credential_id).values(**values))

    @staticmethod
    async def mark_dead(session: AsyncSession, credential_id: int, error: str) -> bool:
        row = await session.scalar(
            select(Credential).where(Credential.id == credential_id).with_for_update()
        )
        if not row:
            return False
        transitioned = row.status != CredentialStatus.DEAD
        row.status = CredentialStatus.DEAD
        row.cooldown_until = None
        row.last_error = error[:4000]
        row.last_health_check_at = utcnow()
        row.health_failures += 1
        if transitioned:
            row.dead_since = utcnow()
            row.dead_notified_at = None
        return transitioned

    @staticmethod
    async def mark_dead_notified(session: AsyncSession, credential_id: int) -> None:
        await session.execute(
            update(Credential)
            .where(Credential.id == credential_id, Credential.status == CredentialStatus.DEAD)
            .values(dead_notified_at=utcnow())
        )

    @staticmethod
    async def unreported_dead(session: AsyncSession, limit: int = 100) -> list[Credential]:
        return list(
            await session.scalars(
                select(Credential)
                .where(
                    Credential.status == CredentialStatus.DEAD,
                    Credential.dead_notified_at.is_(None),
                )
                .order_by(Credential.dead_since.nullsfirst(), Credential.id)
                .limit(limit)
            )
        )

    @staticmethod
    async def cooldown(
        session: AsyncSession,
        credential_id: int,
        seconds: int,
        error: str,
        *,
        limited: bool = False,
    ) -> None:
        await session.execute(
            update(Credential)
            .where(Credential.id == credential_id)
            .values(
                status=CredentialStatus.LIMITED if limited else CredentialStatus.COOLDOWN,
                cooldown_until=utcnow() + timedelta(seconds=max(1, seconds)),
                last_error=error[:4000],
                last_health_check_at=utcnow(),
                health_failures=Credential.health_failures + 1,
            )
        )

    @staticmethod
    async def counts(session: AsyncSession) -> dict[str, int]:
        rows = await session.execute(
            select(Credential.platform, Credential.status, func.count()).group_by(
                Credential.platform, Credential.status
            )
        )
        return {f"{p.value}:{s.value}": int(c) for p, s, c in rows}


class ProxyRepository:
    @staticmethod
    async def add(
        session: AsyncSession,
        *,
        canonical_url: str,
        encrypted_url: str,
        display: str,
        scheme: str,
        country_code: str,
        external_ip: str,
        latency_ms: int,
    ) -> tuple[Proxy, bool]:
        url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()
        existing = await session.scalar(select(Proxy).where(Proxy.canonical_url_hash == url_hash))
        if existing:
            existing.proxy_url_encrypted = encrypted_url
            existing.display = display
            existing.scheme = scheme
            existing.country_code = country_code
            existing.external_ip = external_ip
            existing.latency_ms = latency_ms
            existing.status = ProxyStatus.HEALTHY
            existing.failures = 0
            existing.last_error = ""
            existing.last_check_at = utcnow()
            existing.last_success_at = utcnow()
            return existing, False
        row = Proxy(
            canonical_url_hash=url_hash,
            proxy_url_encrypted=encrypted_url,
            display=display,
            scheme=scheme,
            country_code=country_code,
            external_ip=external_ip,
            latency_ms=latency_ms,
            status=ProxyStatus.HEALTHY,
            successes=1,
            last_check_at=utcnow(),
            last_success_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        return row, True

    @staticmethod
    async def available(session: AsyncSession, limit: int = 100) -> list[Proxy]:
        now = utcnow()
        rows = await session.scalars(
            select(Proxy)
            .where(
                or_(
                    Proxy.status.in_([ProxyStatus.HEALTHY, ProxyStatus.DEGRADED]),
                    and_(Proxy.status == ProxyStatus.QUARANTINE, Proxy.quarantine_until <= now),
                )
            )
            .order_by(Proxy.failures, Proxy.latency_ms.nullslast(), Proxy.last_check_at.nullsfirst())
            .limit(limit)
        )
        return list(rows)

    @staticmethod
    async def mark_success(session: AsyncSession, proxy_id: int, latency_ms: int | None = None) -> None:
        values: dict[str, Any] = {
            "status": ProxyStatus.HEALTHY,
            "failures": 0,
            "successes": Proxy.successes + 1,
            "last_error": "",
            "last_check_at": utcnow(),
            "last_success_at": utcnow(),
            "quarantine_until": None,
        }
        if latency_ms is not None:
            values["latency_ms"] = latency_ms
        await session.execute(update(Proxy).where(Proxy.id == proxy_id).values(**values))

    @staticmethod
    async def mark_failure(
        session: AsyncSession,
        proxy_id: int,
        error: str,
        *,
        quarantine_after: int,
        remove_after: int,
        quarantine_minutes: int,
        remove_after_hours: int = 3,
        immediate_remove: bool = False,
    ) -> ProxyStatus:
        row = await session.get(Proxy, proxy_id)
        if not row:
            return ProxyStatus.REMOVED
        now = utcnow()
        row.failures += 1
        row.last_error = error[:4000]
        row.last_check_at = now
        last_known_good = row.last_success_at or (row.created_at if row.successes == 0 else None)
        failed_too_long = bool(
            last_known_good
            and last_known_good <= now - timedelta(hours=max(1, remove_after_hours))
        )
        if immediate_remove or failed_too_long:
            row.status = ProxyStatus.REMOVED
            row.quarantine_until = None
        elif row.failures >= max(1, quarantine_after):
            row.status = ProxyStatus.QUARANTINE
            row.quarantine_until = now + timedelta(minutes=quarantine_minutes)
        else:
            row.status = ProxyStatus.DEGRADED
        return row.status

    @staticmethod
    async def counts(session: AsyncSession) -> dict[str, int]:
        rows = await session.execute(select(Proxy.status, func.count()).group_by(Proxy.status))
        return {status.value: int(count) for status, count in rows}


class AlertRepository:
    @staticmethod
    async def should_send(
        session: AsyncSession,
        alert_key: str,
        *,
        active: bool,
        payload: dict[str, Any],
        cooldown_minutes: int,
        repeat_while_active: bool = False,
        send_recovery: bool = False,
    ) -> bool:
        row = await session.get(AlertState, alert_key)
        now = utcnow()
        if row is None:
            row = AlertState(alert_key=alert_key, active=active, payload=payload)
            session.add(row)
            await session.flush()
            return active
        changed = row.active != active
        cooldown_passed = not row.last_sent_at or row.last_sent_at <= now - timedelta(minutes=cooldown_minutes)
        row.active = active
        row.payload = payload
        if active and changed:
            return True
        if active and repeat_while_active and cooldown_passed:
            return True
        if not active and changed and send_recovery:
            return True
        return False

    @staticmethod
    async def mark_sent(session: AsyncSession, alert_key: str) -> None:
        row = await session.get(AlertState, alert_key)
        if row:
            row.last_sent_at = utcnow()
