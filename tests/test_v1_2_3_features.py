from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from app.collectors.errors import CredentialDeadError
from app.collectors.telegram import TelegramCollector
from app.collectors.vk import VkApiError, VkCollector
from app.db.enums import ItemType, Platform
from app.services.delivery import DeliveryWorker
from app.services.image_preview import prepare_preview


class PhotoStrippedSize:
    def __init__(self, data: bytes = b"tiny"):
        self.bytes = data
        self.w = 40
        self.h = 40


class PhotoSize:
    def __init__(self, width: int, height: int, size: int):
        self.w = width
        self.h = height
        self.size = size


def _settings(tmp_path: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        media_max_image_edge=1600,
        media_max_preview_bytes=3_000_000,
        media_max_download_bytes=12_000_000,
        media_max_previews_per_item=4,
        media_min_preview_edge=320,
        media_root=tmp_path or Path("data/media"),
        vk_per_token_min_interval_seconds=0,
        vk_api_version="5.131",
        vk_api_base="https://api.vk.com/method",
    )


def _jpeg(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (40, 90, 160))
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def test_prepare_preview_keeps_high_resolution_within_limits() -> None:
    preview = prepare_preview(
        _jpeg(3200, 2000),
        max_edge=1600,
        max_bytes=3_000_000,
    )

    assert preview is not None
    assert preview.mime_type == "image/jpeg"
    assert max(preview.width, preview.height) == 1600
    assert len(preview.data) <= 3_000_000
    with Image.open(BytesIO(preview.data)) as decoded:
        assert decoded.size == (preview.width, preview.height)
        assert decoded.format == "JPEG"


def test_telegram_real_thumbnails_outrank_stripped_placeholder() -> None:
    collector = TelegramCollector(_settings())  # type: ignore[arg-type]
    stripped = PhotoStrippedSize()
    small = PhotoSize(320, 180, 25_000)
    large = PhotoSize(1280, 720, 240_000)
    media = SimpleNamespace(sizes=[stripped, small, large])

    assert collector._thumb_candidates(media) == [large, small]


@pytest.mark.asyncio
async def test_telegram_download_tries_largest_real_thumbnail_first() -> None:
    collector = TelegramCollector(_settings())  # type: ignore[arg-type]
    large = PhotoSize(1280, 720, 240_000)
    small = PhotoSize(320, 180, 25_000)
    media = SimpleNamespace(sizes=[PhotoStrippedSize(), small, large])

    class FakeClient:
        calls: list[Any] = []

        async def download_media(self, _target: Any, *, file: type[bytes], thumb: Any = None) -> bytes:
            assert file is bytes
            self.calls.append(thumb)
            return _jpeg(1280, 720)

    client = FakeClient()
    preview = await collector._download_preview_bytes(client, media, download_target=object())  # type: ignore[arg-type]

    assert preview is not None
    assert client.calls == [large]
    assert preview.width == 1280
    assert preview.height == 720


class FakeBot:
    def __init__(self) -> None:
        self.photo_calls: list[dict[str, Any]] = []
        self.message_calls: list[dict[str, Any]] = []

    async def send_photo(self, chat_id: int, **kwargs: Any) -> SimpleNamespace:
        self.photo_calls.append({"chat_id": chat_id, **kwargs})
        return SimpleNamespace(message_id=101)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        self.message_calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=102)


def _delivery(tmp_path: Path, text: str) -> SimpleNamespace:
    image_path = tmp_path / "preview.jpg"
    image_path.write_bytes(_jpeg(640, 360))
    source = SimpleNamespace(
        title="Тестовая группа",
        normalized_link="https://t.me/example",
        category="",
        subcategory="",
        region="",
        federal_district="",
    )
    media = SimpleNamespace(local_path=str(image_path), media_type="photo", position=0)
    item = SimpleNamespace(
        source=source,
        platform=Platform.TELEGRAM,
        item_type=ItemType.POST,
        published_at=None,
        created_at=None,
        text=text,
        original_url="https://t.me/example/1",
        media=[media],
    )
    return SimpleNamespace(id=1, target_chat_id=123, item=item)


@pytest.mark.asyncio
async def test_long_single_media_post_does_not_repeat_text_in_caption(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = FakeBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Длинный текст " * 250)

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert message_ids == [101, 102]
    assert len(worker.bot.photo_calls) == 1  # type: ignore[attr-defined]
    assert worker.bot.photo_calls[0]["caption"] is None  # type: ignore[attr-defined]
    assert len(worker.bot.message_calls) == 1  # type: ignore[attr-defined]
    assert worker.bot.message_calls[0]["text"].count("Длинный текст") > 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_short_single_media_post_uses_one_caption_only(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = FakeBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Короткий текст")

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert message_ids == [101]
    assert "Короткий текст" in worker.bot.photo_calls[0]["caption"]  # type: ignore[attr-defined]
    assert worker.bot.message_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_vk_1116_is_dead_and_error_does_not_leak_token() -> None:
    secret = "vk1.a.secret-token-value"
    collector = VkCollector(_settings())  # type: ignore[arg-type]

    class Response:
        status = 200

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def text(self) -> str:
            return json.dumps(
                {
                    "error": {
                        "error_code": 1116,
                        "error_msg": f"Anonymous token {secret} invalid",
                    }
                }
            )

    class Session:
        def post(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

    with pytest.raises(CredentialDeadError) as caught:
        await collector._call(Session(), None, secret, "wall.get", {})  # type: ignore[arg-type]

    assert secret not in str(caught.value)
    assert "<redacted>" in str(caught.value)
    assert secret not in str(VkApiError({"error_code": 1116, "error_msg": f"token {secret} invalid"}))


@pytest.mark.asyncio
async def test_telegram_video_cover_is_preferred_without_video_download() -> None:
    collector = TelegramCollector(_settings())  # type: ignore[arg-type]
    cover_size = PhotoSize(1440, 810, 300_000)
    cover = SimpleNamespace(sizes=[cover_size])
    document = SimpleNamespace(thumbs=[PhotoSize(320, 180, 20_000)], size=50_000_000)
    media = SimpleNamespace(video_cover=cover)
    message = SimpleNamespace(
        id=50,
        media=media,
        photo=None,
        video=object(),
        document=document,
    )

    class FakeClient:
        calls: list[tuple[Any, Any]] = []

        async def download_media(self, target: Any, *, file: type[bytes], thumb: Any = None) -> bytes:
            assert file is bytes
            self.calls.append((target, thumb))
            return _jpeg(1440, 810)

    client = FakeClient()
    media_obj, target, media_type, allow_full = collector._message_media(message)
    preview = await collector._download_preview_bytes(  # type: ignore[arg-type]
        client,
        media_obj,
        download_target=target,
        allow_full_image=allow_full,
    )

    assert media_obj is cover
    assert target is cover
    assert media_type == "video_preview"
    assert allow_full is False
    assert preview is not None
    assert client.calls == [(cover, cover_size)]


@pytest.mark.asyncio
async def test_unknown_size_image_document_never_downloads_full_payload() -> None:
    collector = TelegramCollector(_settings())  # type: ignore[arg-type]
    thumb = PhotoSize(1280, 720, 180_000)
    document = SimpleNamespace(thumbs=[thumb], size=0)

    class FakeClient:
        calls: list[Any] = []

        async def download_media(self, _target: Any, *, file: type[bytes], thumb: Any = None) -> bytes:
            assert file is bytes
            self.calls.append(thumb)
            assert thumb is not None, "unknown-size image document must not be downloaded in full"
            return _jpeg(1280, 720)

    client = FakeClient()
    preview = await collector._download_preview_bytes(  # type: ignore[arg-type]
        client,
        document,
        download_target=object(),
        allow_full_image=True,
    )

    assert preview is not None
    assert client.calls == [thumb]


def test_vk_image_candidates_are_ranked_largest_first() -> None:
    collector = VkCollector(_settings())  # type: ignore[arg-type]
    photo = {
        "sizes": [
            {"url": "small", "width": 320, "height": 200},
            {"url": "large", "width": 2560, "height": 1600},
            {"url": "medium", "width": 1280, "height": 800},
        ]
    }

    candidates = collector._photo_candidates(photo)
    media = collector._media_from_attachments([{"type": "photo", "photo": photo}])

    assert [row["url"] for row in candidates] == ["large", "medium", "small"]
    assert media[0].preview_url == "large"
    assert media[0].metadata["preview_candidates"] == ["large", "medium", "small"]


def test_vk_secret_redaction_handles_raw_and_named_token() -> None:
    secret = "vk1.a.really-secret-value"

    direct = str(VkApiError({"error_code": 1116, "error_msg": f"credential {secret} rejected"}, secret))
    named = str(VkApiError({"error_code": 1116, "error_msg": f"access_token={secret} rejected"}))

    assert secret not in direct
    assert secret not in named
    assert "<redacted>" in direct
    assert "<redacted>" in named


def test_vk_repost_deduplicates_same_text_and_attachments() -> None:
    collector = VkCollector(_settings())  # type: ignore[arg-type]
    photo = {
        "type": "photo",
        "photo": {
            "owner_id": -10,
            "id": 55,
            "sizes": [{"url": "https://img/large", "width": 1280, "height": 720}],
        },
    }
    video = {
        "type": "video",
        "video": {
            "owner_id": -10,
            "id": 77,
            "image": [{"url": "https://img/video", "width": 1280, "height": 720}],
        },
    }

    merged = collector._merge_attachments([photo], [photo, video])

    assert merged == [photo, video]
    assert collector._merge_repost_text("Одинаковый текст", "Одинаковый текст") == "Одинаковый текст"
    assert collector._merge_repost_text("Комментарий", "Оригинал") == "Комментарий\n\nОригинал"


def test_tiny_placeholder_is_rejected_instead_of_upscaled() -> None:
    preview = prepare_preview(
        _jpeg(40, 40),
        max_edge=1600,
        max_bytes=3_000_000,
        min_edge=320,
    )

    assert preview is None
