from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
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
    return datetime.now(timezone.utc)


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
        title: str = "",
        external_id: str = "",
        metadata: dict[str, Any] | None = None,
        next_check_at: datetime | None = None,
    ) -> tuple[Source, bool]:
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
            if title and not existing.title:
                existing.title = title
            return existing, False

        source = Source(
            platform=platform,
            input_link=input_link,
            normalized_link=normalized_link,
            region=region,
            federal_district=federal_district,
            added_by=added_by,
            title=title,
            external_id=external_id,
            metadata_json=metadata or {},
            next_check_at=next_check_at or utcnow(),
        )
        source.state = SourceState()
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
            existing = await session.scalars(
                select(Source).where(tuple_(Source.platform, Source.normalized_link).in_(keys))
            )
            existing_map.update({(row.platform, row.normalized_link): row for row in existing})
        created = updated = 0
        for row in rows:
            platform = Platform(row["platform"])
            key = (platform, row["normalized_link"])
            existing = existing_map.get(key)
            if existing:
                if existing.status == SourceStatus.DELETED:
                    existing.status = SourceStatus.ACTIVE
                if row.get("region"):
                    existing.region = row["region"]
                if row.get("federal_district"):
                    existing.federal_district = row["federal_district"]
                if row.get("title"):
                    existing.title = row["title"][:500]
                if row.get("external_id"):
                    existing.external_id = str(row["external_id"])[:255]
                updated += 1
                continue
            source = Source(
                platform=platform,
                input_link=row["input_link"],
                normalized_link=row["normalized_link"],
                region=row.get("region", ""),
                federal_district=row.get("federal_district", ""),
                added_by=added_by,
                title=row.get("title", ""),
                external_id=str(row.get("external_id", "")),
                metadata_json=row.get("metadata", {}),
                next_check_at=row.get("next_check_at") or utcnow(),
            )
            source.state = SourceState()
            session.add(source)
            existing_map[key] = source
            created += 1
        await session.flush()
        return created, updated

    @staticmethod
    async def get(session: AsyncSession, source_id: int) -> Source | None:
        return await session.scalar(
            select(Source)
            .where(Source.id == source_id)
            .options(selectinload(Source.state))
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
    ) -> tuple[list[Source], int]:
        filters = [Source.status != SourceStatus.DELETED]
        if query:
            token = f"%{query.strip()}%"
            filters.append(
                or_(
                    Source.title.ilike(token),
                    Source.normalized_link.ilike(token),
                    Source.region.ilike(token),
                )
            )
        if platform:
            filters.append(Source.platform == platform)
        if status:
            filters.append(Source.status == status)
        total = int(await session.scalar(select(func.count()).select_from(Source).where(*filters)) or 0)
        rows = await session.scalars(
            select(Source)
            .where(*filters)
            .order_by(Source.region, Source.title, Source.id)
            .offset(max(0, page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total

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
            .values(region=region, federal_district=federal_district)
        )

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
    ) -> None:
        source = await session.get(Source, source_id)
        if not source:
            return
        source.last_check_at = utcnow()
        source.last_success_at = utcnow()
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
        return int(result.rowcount or 0)

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
        bootstrap: bool,
        signal_chat_id: int | None,
    ) -> tuple[int, int]:
        inserted = 0
        deliveries = 0
        for payload in items:
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
            if not bootstrap and signal_chat_id:
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
    ) -> None:
        state = await session.get(SourceState, source_id)
        if not state:
            state = SourceState(source_id=source_id)
            session.add(state)
        if bootstrap_completed is not None:
            state.bootstrap_completed = bootstrap_completed
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
        return int(result.rowcount or 0)

    @staticmethod
    async def claim(session: AsyncSession, lease_seconds: int) -> Delivery | None:
        now = utcnow()
        delivery = await session.scalar(
            select(Delivery)
            .where(
                Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRY]),
                Delivery.run_after <= now,
            )
            .order_by(Delivery.run_after, Delivery.id)
            .with_for_update(skip_locked=True)
            .options(
                selectinload(Delivery.item).selectinload(Item.source),
                selectinload(Delivery.item).selectinload(Item.media),
            )
            .limit(1)
        )
        if not delivery:
            return None
        delivery.status = DeliveryStatus.RUNNING
        delivery.locked_until = now + timedelta(seconds=lease_seconds)
        delivery.attempts += 1
        await session.flush()
        return delivery

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
                        Credential.status == CredentialStatus.COOLDOWN,
                        Credential.cooldown_until <= now,
                    ),
                ),
            )
            .order_by(Credential.requests_count, Credential.last_success_at.nullsfirst())
            .limit(limit)
        )
        return list(rows)

    @staticmethod
    async def mark_success(session: AsyncSession, credential_id: int) -> None:
        await session.execute(
            update(Credential)
            .where(Credential.id == credential_id)
            .values(
                status=CredentialStatus.ACTIVE,
                cooldown_until=None,
                last_success_at=utcnow(),
                last_error="",
                requests_count=Credential.requests_count + 1,
            )
        )

    @staticmethod
    async def mark_dead(session: AsyncSession, credential_id: int, error: str) -> None:
        await session.execute(
            update(Credential)
            .where(Credential.id == credential_id)
            .values(status=CredentialStatus.DEAD, last_error=error[:4000])
        )

    @staticmethod
    async def cooldown(
        session: AsyncSession, credential_id: int, seconds: int, error: str
    ) -> None:
        await session.execute(
            update(Credential)
            .where(Credential.id == credential_id)
            .values(
                status=CredentialStatus.COOLDOWN,
                cooldown_until=utcnow() + timedelta(seconds=max(1, seconds)),
                last_error=error[:4000],
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
        immediate_remove: bool = False,
    ) -> ProxyStatus:
        row = await session.get(Proxy, proxy_id)
        if not row:
            return ProxyStatus.REMOVED
        row.failures += 1
        row.last_error = error[:4000]
        row.last_check_at = utcnow()
        if immediate_remove or row.failures >= remove_after:
            row.status = ProxyStatus.REMOVED
            row.quarantine_until = None
        elif row.failures >= quarantine_after:
            row.status = ProxyStatus.QUARANTINE
            row.quarantine_until = utcnow() + timedelta(minutes=quarantine_minutes)
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
        if changed or (active and cooldown_passed):
            return True
        return False

    @staticmethod
    async def mark_sent(session: AsyncSession, alert_key: str) -> None:
        row = await session.get(AlertState, alert_key)
        if row:
            row.last_sent_at = utcnow()
