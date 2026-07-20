from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.collectors.telegram import TelegramCollector
from app.db.enums import ItemType, Platform
from app.services.delivery import DeliveryWorker
from app.services.importer import parse_delimited


def test_delivery_header_is_compact_and_uses_moscow_publication_time() -> None:
    delivery = SimpleNamespace(
        item=SimpleNamespace(
            platform=Platform.TELEGRAM,
            item_type=ItemType.POST,
            published_at=datetime(2026, 7, 18, 17, 18, tzinfo=UTC),
            created_at=None,
            text="Тестовый текст",
            source=SimpleNamespace(
                title="Тестовая группа",
                normalized_link="https://t.me/test",
                category="ЦФО",
                subcategory="Москва",
                region="",
                federal_district="",
            ),
        )
    )

    header = DeliveryWorker._header(delivery)

    assert header.startswith("<b>🔵 TG · ПОСТ · 18.07.2026 20:18</b>")
    assert "НОВЫЙ" not in header
    assert "TELEGRAM" not in header
    assert "Тестовый текст" in header


def test_vk_story_header_uses_platform_icon() -> None:
    delivery = SimpleNamespace(
        item=SimpleNamespace(
            platform=Platform.VK,
            item_type=ItemType.STORY,
            published_at=datetime(2026, 7, 18, 0, 0, tzinfo=UTC),
            created_at=None,
            text="",
            source=SimpleNamespace(
                title="VK source",
                normalized_link="https://vk.com/test",
                category="",
                subcategory="",
                region="",
                federal_district="",
            ),
        )
    )

    assert DeliveryWorker._header(delivery).startswith("<b>🟢 VK · ИСТОРИЯ · 18.07.2026 03:00</b>")


def test_importer_supports_generic_categories(tmp_path: Path) -> None:
    path = tmp_path / "sources.csv"
    path.write_text(
        "Категория;Подкатегория;Ссылка;Название\nПроект А;Направление 1;https://t.me/example_name;Example\n",
        encoding="utf-8",
    )

    preview = parse_delimited(path)

    assert len(preview.candidates) == 1
    candidate = preview.candidates[0]
    assert candidate.category == "Проект А"
    assert candidate.subcategory == "Направление 1"


def test_v1_2_migration_uses_native_enum_member_name() -> None:
    migration = Path("alembic/versions/0003_unified_delivery_health_categories.py").read_text()
    assert "ADD VALUE IF NOT EXISTS 'LIMITED'" in migration
    assert "category" in migration
    assert "last_health_ok_at" in migration
    assert "last_success_at" in migration


def test_compose_uses_one_shared_image_and_rotated_logs() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    services = compose["services"]
    app_services = ["migrate", "bot", "scheduler", "worker-vk", "worker-tg", "delivery"]
    assert {services[name]["image"] for name in app_services} == {"social-monitor-bot:local"}
    for name in ["postgres", *app_services]:
        assert services[name]["logging"]["options"] == {"max-size": "10m", "max-file": "3"}


@pytest.mark.asyncio
async def test_telegram_video_without_thumbnail_is_never_downloaded_as_full_video(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        media_root=tmp_path,
        media_max_previews_per_item=4,
        media_max_preview_bytes=2_000_000,
        media_max_image_edge=1280,
    )
    collector = TelegramCollector(settings)
    document = SimpleNamespace(thumbs=[])
    message = SimpleNamespace(id=7, photo=None, document=document, video=True, media=object())

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def download_media(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return b"full-video-that-must-not-be-used"

    client = FakeClient()
    result = await collector._download_message_previews(client, "tg:test", [message])

    assert result == []
    assert client.calls == []


def test_vk_captcha_and_rate_limits_are_not_dead_tokens() -> None:
    from app.collectors.vk import RETRY_CODES, TOKEN_DEAD_CODES

    assert 14 in RETRY_CODES
    assert 14 not in TOKEN_DEAD_CODES
    assert TOKEN_DEAD_CODES.isdisjoint(RETRY_CODES)


def test_telegram_revoked_session_and_flood_wait_are_classified_separately() -> None:
    from app.collectors.errors import CredentialDeadError, RateLimitedError
    from app.collectors.telegram import _classify

    SessionRevoked = type("SessionRevokedError", (Exception,), {})
    FloodWait = type("FloodWaitError", (Exception,), {"seconds": 900})

    dead = _classify(SessionRevoked("revoked"))
    limited = _classify(FloodWait("wait"))

    assert isinstance(dead, CredentialDeadError)
    assert isinstance(limited, RateLimitedError)
    assert limited.retry_after == 900
