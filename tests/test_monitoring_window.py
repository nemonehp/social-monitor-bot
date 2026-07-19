from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.collectors.telegram import TelegramCollector
from app.collectors.vk import VkCollector
from app.db.enums import Platform
from app.db.models import Source, SourceState


def make_source(*, started: datetime, checkpoint: datetime, completed: bool) -> Source:
    source = Source(
        platform=Platform.TELEGRAM,
        input_link="https://t.me/example",
        normalized_link="https://t.me/example",
        added_by=1,
        next_check_at=started,
    )
    source.created_at = started
    source.state = SourceState(
        monitor_from_at=started,
        checkpoint_at=checkpoint,
        bootstrap_completed=completed,
    )
    return source


@pytest.mark.parametrize("collector_cls", [TelegramCollector, VkCollector])
def test_first_run_starts_at_monitor_boundary(collector_cls):
    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    source = make_source(started=started, checkpoint=started, completed=False)
    collector = collector_cls(SimpleNamespace(collection_overlap_seconds=120))

    window_start, window_end, first_run = collector._window(source)

    assert first_run is True
    assert window_start == started
    assert window_end >= started


@pytest.mark.parametrize("collector_cls", [TelegramCollector, VkCollector])
def test_regular_run_uses_checkpoint_with_overlap(collector_cls):
    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    checkpoint = started + timedelta(minutes=10)
    source = make_source(started=started, checkpoint=checkpoint, completed=True)
    collector = collector_cls(SimpleNamespace(collection_overlap_seconds=120))

    window_start, _window_end, first_run = collector._window(source)

    assert first_run is False
    assert window_start == checkpoint - timedelta(seconds=120)


@pytest.mark.parametrize("collector_cls", [TelegramCollector, VkCollector])
def test_paginated_run_keeps_frozen_window_end(collector_cls):
    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    frozen = started + timedelta(minutes=5)
    source = make_source(started=started, checkpoint=started, completed=False)
    source.state.post_cursor = {"window_end": frozen.isoformat()}
    collector = collector_cls(SimpleNamespace(collection_overlap_seconds=120))

    _window_start, window_end, _first_run = collector._window(source)

    assert window_end == frozen


@pytest.mark.asyncio
async def test_telegram_video_downloads_thumbnail_only(tmp_path):
    settings = SimpleNamespace(
        media_root=tmp_path,
        media_max_previews_per_item=4,
        media_max_preview_bytes=2_000_000,
        media_max_image_edge=1280,
    )
    collector = TelegramCollector(settings)

    small = SimpleNamespace(w=640, h=360)
    huge = SimpleNamespace(w=1920, h=1080)
    document = SimpleNamespace(thumbs=[small, huge])
    message = SimpleNamespace(
        id=10,
        media=object(),
        photo=None,
        document=document,
        video=True,
    )

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def download_media(self, media, *, file, thumb):
            self.calls.append((media, file, thumb))
            buffer = BytesIO()
            Image.new("RGB", (1920, 1080), "white").save(buffer, format="JPEG")
            return buffer.getvalue()

    client = FakeClient()
    result = await collector._download_message_previews(client, "tg:test", [message])

    assert len(result) == 1
    assert result[0].media_type == "video_preview"
    assert result[0].metadata["preview_only"] is True
    assert client.calls == [(message, bytes, huge)]


@pytest.mark.asyncio
async def test_telegram_first_scan_ignores_history_and_does_not_skip_future(monkeypatch, tmp_path):
    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    frozen = started + timedelta(minutes=5)
    source = make_source(started=started, checkpoint=started, completed=False)
    source.state.post_cursor = {"window_end": frozen.isoformat()}
    settings = SimpleNamespace(
        collection_overlap_seconds=120,
        tg_batch_messages=500,
        max_credential_tries_per_source=5,
        media_root=tmp_path,
        media_max_previews_per_item=4,
        media_max_preview_bytes=2_000_000,
        media_max_image_edge=1280,
    )
    collector = TelegramCollector(settings)

    async def no_stories(_client, _entity):
        return []

    monkeypatch.setattr(collector, "_collect_active_stories", no_stories)

    def message(message_id: int, published: datetime):
        return SimpleNamespace(
            id=message_id,
            date=published,
            message=f"message {message_id}",
            raw_text=f"message {message_id}",
            grouped_id=None,
            media=None,
            photo=None,
            document=None,
            video=None,
        )

    class FakeClient:
        async def get_entity(self, _username):
            return SimpleNamespace(id=999, title="Example", username="example")

        async def get_messages(self, _entity, *, limit, offset_id):
            assert limit == 500
            assert offset_id == 0
            return [
                message(12, frozen + timedelta(minutes=1)),
                message(11, frozen - timedelta(minutes=1)),
                message(10, started - timedelta(seconds=1)),
            ]

    result = await collector.collect(source, FakeClient())

    assert [item.external_id for item in result.items] == ["11"]
    assert result.post_watermark == "11"
    assert result.needs_immediate_retry is False


@pytest.mark.asyncio
async def test_telegram_regular_scan_does_not_advance_past_frozen_end(monkeypatch, tmp_path):
    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    checkpoint = started + timedelta(minutes=5)
    frozen = checkpoint + timedelta(minutes=5)
    source = make_source(started=started, checkpoint=checkpoint, completed=True)
    source.state.post_watermark = "10"
    source.state.post_cursor = {"window_end": frozen.isoformat()}
    settings = SimpleNamespace(
        collection_overlap_seconds=120,
        tg_batch_messages=500,
        max_credential_tries_per_source=5,
        media_root=tmp_path,
        media_max_previews_per_item=4,
        media_max_preview_bytes=2_000_000,
        media_max_image_edge=1280,
    )
    collector = TelegramCollector(settings)

    async def no_stories(_client, _entity):
        return []

    monkeypatch.setattr(collector, "_collect_active_stories", no_stories)

    def message(message_id: int, published: datetime):
        return SimpleNamespace(
            id=message_id,
            date=published,
            message=f"message {message_id}",
            raw_text=f"message {message_id}",
            grouped_id=None,
            media=None,
            photo=None,
            document=None,
            video=None,
        )

    class FakeClient:
        async def get_entity(self, _username):
            return SimpleNamespace(id=999, title="Example", username="example")

        def iter_messages(self, _entity, *, min_id, reverse, limit):
            assert min_id == 10
            assert reverse is True
            assert limit == 520

            async def iterator():
                yield message(11, frozen - timedelta(minutes=1))
                yield message(12, frozen + timedelta(minutes=1))

            return iterator()

    result = await collector.collect(source, FakeClient())

    assert [item.external_id for item in result.items] == ["11"]
    assert result.post_watermark == "11"


@pytest.mark.asyncio
async def test_vk_scan_does_not_advance_past_frozen_end(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager

    started = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    frozen = started + timedelta(minutes=5)
    source = make_source(started=started, checkpoint=started, completed=False)
    source.platform = Platform.VK
    source.input_link = "https://vk.com/example"
    source.normalized_link = "https://vk.com/example"
    source.state.post_cursor = {"window_end": frozen.isoformat()}
    settings = SimpleNamespace(
        collection_overlap_seconds=120,
        vk_max_pages_per_run=20,
        vk_page_size=100,
        max_credential_tries_per_source=5,
        media_max_previews_per_item=4,
        media_max_preview_bytes=2_000_000,
        media_max_image_edge=1280,
        media_root=tmp_path,
    )
    collector = VkCollector(settings)

    @asynccontextmanager
    async def fake_proxy_session(_proxy_url, *, timeout_seconds):
        assert timeout_seconds == 60
        yield object(), "http://proxy"

    async def fake_resolve_owner(_session, _request_proxy, _token, _source):
        return -999, "group", "Example", "example"

    def post(post_id: int, published: datetime):
        return {
            "id": post_id,
            "owner_id": -999,
            "date": int(published.timestamp()),
            "text": f"post {post_id}",
            "attachments": [],
        }

    async def fake_call(_session, _request_proxy, _token, method, _params):
        if method == "wall.get":
            return {
                "response": {
                    "items": [
                        post(12, frozen + timedelta(minutes=1)),
                        post(11, frozen - timedelta(minutes=1)),
                        post(10, started - timedelta(seconds=1)),
                    ]
                }
            }
        if method == "stories.get":
            return {"response": {"items": []}}
        raise AssertionError(method)

    monkeypatch.setattr("app.collectors.vk.proxy_session", fake_proxy_session)
    monkeypatch.setattr(collector, "_resolve_owner", fake_resolve_owner)
    monkeypatch.setattr(collector, "_call", fake_call)

    result = await collector.collect(source, token="token", proxy_url="http://proxy")

    assert [item.external_id for item in result.items] == ["-999_11"]
    assert result.post_watermark == "11"
    assert result.needs_immediate_retry is False
