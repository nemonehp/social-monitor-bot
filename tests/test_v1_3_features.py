from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.db.enums import ItemType, Platform
from app.services.account_importer import parse_vk_accounts
from app.services.capacity import (
    adaptive_account_budget,
    estimate_requests_per_day,
    safe_budget_per_account,
)
from app.services.delivery import DeliveryWorker
from app.services.forum_topics import TOPIC_SPECS
from app.services.image_preview import PreparedPreview, decorate_video_preview
from app.services.vk_assignments import VkAssignment, plan_proxy_affinity
from app.utils.text import vk_text_to_html
from app.workers.vk_worker import AssignmentPool


def _capacity_settings() -> SimpleNamespace:
    return SimpleNamespace(
        account_daily_budget_fraction=0.30,
        vk_operational_daily_request_budget=100_000,
        tg_operational_daily_request_budget=250_000,
        vk_estimated_requests_per_source_cycle=2.25,
        tg_estimated_requests_per_source_cycle=2.0,
    )


def test_capacity_model_uses_thirty_percent_and_actual_interval() -> None:
    settings = _capacity_settings()
    assert safe_budget_per_account(Platform.VK, settings) == 30_000
    assert safe_budget_per_account(Platform.TELEGRAM, settings) == 75_000
    assert (
        estimate_requests_per_day(
            platform=Platform.VK,
            source_count=84,
            interval_seconds=120,
            settings=settings,
        )
        == 136_080
    )
    assert adaptive_account_budget(Platform.VK, settings, rate_limit_events=3) == 12_000


def test_vk_proxy_plan_never_exceeds_three_accounts_per_ip() -> None:
    credentials = [(index, None, index * 10) for index in range(1, 11)]
    proxies = [(101, "1.1.1.1"), (102, "1.1.1.1"), (201, "2.2.2.2"), (301, "3.3.3.3")]
    result = plan_proxy_affinity(credentials, proxies, max_accounts_per_ip=3)
    proxy_ip = {101: "1.1.1.1", 102: "1.1.1.1", 201: "2.2.2.2", 301: "3.3.3.3"}
    counts = Counter(proxy_ip[proxy_id] for proxy_id in result.values())
    assert len(result) == 9
    assert all(count <= 3 for count in counts.values())


@pytest.mark.asyncio
async def test_vk_source_stays_in_stable_account_group() -> None:
    pool = AssignmentPool()
    rows = [
        VkAssignment(index, f"vk-{index}", f"token-{index}", 100 + index, "http://proxy", f"1.1.1.{index}")
        for index in range(1, 5)
    ]
    await pool.replace(rows)
    first = await pool.acquire_for(777)
    assert first is not None
    await pool.release(first)
    second = await pool.acquire_for(777)
    assert second is not None
    assert second.credential_id == first.credential_id


def test_vk_oauth_response_preserves_expiry_metadata() -> None:
    accounts, errors = parse_vk_accounts(
        'primary;{"access_token":"' + "a" * 40 + '","expires_in":21600,"user_id":42}'
    )
    assert errors == []
    assert len(accounts) == 1
    account = accounts[0]
    assert account.expires_at is not None
    assert account.expires_at > datetime.now(UTC)
    assert account.config["expires_in"] == 21600
    assert account.config["user_id"] == 42


def test_vk_markup_becomes_safe_telegram_links() -> None:
    rendered = vk_text_to_html("Автор: [https://vk.ru/id285495652|Елена Слобода] и [club123|Сообщество]")
    assert '<a href="https://vk.ru/id285495652">Елена Слобода</a>' in rendered
    assert '<a href="https://vk.com/club123">Сообщество</a>' in rendered


def test_delivery_uses_expandable_text_and_linked_repost() -> None:
    item = SimpleNamespace(
        platform=Platform.VK,
        item_type=ItemType.POST,
        published_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        created_at=None,
        text="Большой текст",
        original_url="https://vk.com/wall-1_2",
        raw_json={
            "monitor_repost": {
                "is_repost": True,
                "title": "Оригинальное сообщество",
                "url": "https://vk.com/wall-5_10",
            },
            "monitor_content_counts": {"video": 3, "photo": 2},
        },
        source=SimpleNamespace(
            title="Источник",
            normalized_link="https://vk.com/source",
            category="",
            subcategory="",
            region="",
            federal_district="",
        ),
    )
    delivery = SimpleNamespace(item=item)
    cards = DeliveryWorker._cards(delivery, [])
    assert "<blockquote expandable>" in cards[0]
    assert '<a href="https://vk.com/wall-5_10">Оригинальное сообщество</a>' in cards[0]
    assert "3 видео (без превью)" in cards[0]
    assert "2 фото (показано 0)" in cards[0]


def test_video_preview_has_visible_player_overlay() -> None:
    source = BytesIO()
    Image.new("RGB", (800, 450), (30, 30, 30)).save(source, format="JPEG")
    decorated = decorate_video_preview(
        PreparedPreview(source.getvalue(), 800, 450), duration=125, index=2, total=3
    )
    with Image.open(BytesIO(decorated.data)) as image:
        center = image.convert("RGB").getpixel((400, 225))
    assert max(center) > 180
    assert decorated.width == 800
    assert decorated.height == 450


def test_required_forum_topics_are_declared() -> None:
    assert {value[0] for value in TOPIC_SPECS.values()} == {
        "🟢 VK · ПОСТЫ",
        "🟢 VK · ИСТОРИИ",
        "🔵 TG · ПОСТЫ",
        "🔵 TG · ИСТОРИИ",
        "🟡 СТАТИСТИКА",
    }


def test_capacity_migration_revision_is_safe_and_complete() -> None:
    migration = Path("alembic/versions/0004_capacity_forum_integrity.py").read_text()
    assert len("0004_capacity_forum_integrity") <= 32
    assert 'revision = "0004_capacity_forum_integrity"' in migration
    assert "api_usage" in migration
    assert "integrity_checks" in migration
    assert "assigned_proxy_id" in migration


def test_frozen_callback_queries_are_not_mutated() -> None:
    handlers = Path("app/bot/handlers.py").read_text()
    assert "callback.data =" not in handlers


@pytest.mark.asyncio
async def test_vk_busy_preferred_account_does_not_cause_random_rotation() -> None:
    pool = AssignmentPool()
    rows = [
        VkAssignment(index, f"vk-{index}", f"token-{index}", 100 + index, "http://proxy", f"1.1.1.{index}")
        for index in range(1, 4)
    ]
    await pool.replace(rows)
    preferred = await pool.acquire_for(991)
    assert preferred is not None
    while_busy = await pool.acquire_for(991, wait_seconds=0.01)
    assert while_busy is None
    await pool.release(preferred)
    again = await pool.acquire_for(991)
    assert again is not None
    assert again.credential_id == preferred.credential_id


def test_integrity_recognizes_latest_message_inside_telegram_album() -> None:
    from app.collectors.types import CollectedItem, CollectionResult
    from app.services.integrity import _result_contains

    item = CollectedItem(
        platform=Platform.TELEGRAM,
        item_type=ItemType.POST,
        item_key="tg:post:1:album:50",
        external_id="album:50",
        original_url="",
        text="",
        published_at=datetime.now(UTC),
        raw={"message_ids": [100, 101, 102]},
    )
    result = CollectionResult(items=[item])
    assert _result_contains(result, ItemType.POST, 102)


def test_unfinished_vk_scan_does_not_commit_watermark() -> None:
    common = Path("app/workers/common.py").read_text()
    assert 'source.platform.value == "vk" and bool(result.post_cursor)' in common
    assert "post_watermark=None if hold_post_watermark" in common
    assert "checkpoint_at = result.window_end if collection_completed else None" in common


def test_forum_setup_is_retry_safe_and_admin_text_is_explicit() -> None:
    service = Path("app/services/forum_topics.py").read_text()
    handlers = Path("app/bot/handlers.py").read_text()
    assert 'pending_key = f"signal_topic_pending:{chat_id}"' in service
    assert "Already created" not in service
    assert "Уже созданные темы сохранены" in service
    assert "супергруппа с включёнными темами" in handlers
    assert "управлением темами" in handlers
