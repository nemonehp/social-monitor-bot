from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.collectors.telegram import _safe_exception_text
from app.db.enums import ItemType, Platform
from app.services.delivery import DeliveryWorker, _fits_caption, _utf16_units, _visible_html_text
from app.services.integrity import _remote_is_missing


class AuditBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_id = 100

    def _message(self) -> SimpleNamespace:
        self.next_id += 1
        return SimpleNamespace(message_id=self.next_id)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("message", {"chat_id": chat_id, "text": text, **kwargs}))
        return self._message()

    async def send_photo(self, chat_id: int, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("photo", {"chat_id": chat_id, **kwargs}))
        return self._message()

    async def send_video(self, chat_id: int, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("video", {"chat_id": chat_id, **kwargs}))
        return self._message()

    async def send_document(self, chat_id: int, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("document", {"chat_id": chat_id, **kwargs}))
        return self._message()

    async def send_media_group(self, chat_id: int, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.append(("media_group", {"chat_id": chat_id, **kwargs}))
        return [self._message() for _ in kwargs["media"]]


class FailingMediaBot(AuditBot):
    async def send_photo(self, chat_id: int, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("photo", {"chat_id": chat_id, **kwargs}))
        raise TelegramBadRequest(
            method=SimpleNamespace(),  # type: ignore[arg-type]
            message="failed to send media",
        )


def _delivery(tmp_path: Path, text: str, media_count: int = 1) -> SimpleNamespace:
    rows = []
    for index in range(media_count):
        path = tmp_path / f"preview-{index}.jpg"
        path.write_bytes(b"jpeg")
        rows.append(SimpleNamespace(local_path=str(path), media_type="photo", position=index))
    source = SimpleNamespace(
        title="Тестовая группа",
        normalized_link="https://t.me/example",
        category="",
        subcategory="",
        region="",
        federal_district="",
    )
    item = SimpleNamespace(
        source=source,
        platform=Platform.TELEGRAM,
        item_type=ItemType.POST,
        published_at=None,
        created_at=None,
        text=text,
        original_url="https://t.me/example/1",
        media=rows,
        raw_json={},
    )
    return SimpleNamespace(id=77, target_chat_id=123, item=item)


def test_caption_limit_counts_visible_utf16_text_not_html_markup() -> None:
    card = f'<a href="https://example.com/{"x" * 3000}">Короткий текст</a> 😀'
    visible = _visible_html_text(card)

    assert visible == "Короткий текст 😀"
    assert _utf16_units(visible) == len("Короткий текст ") + 2
    assert _fits_caption(card)
    assert not _fits_caption("я" * 1025)


@pytest.mark.asyncio
async def test_long_card_is_sent_before_media_and_media_replies_to_it(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = AuditBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Т" * 1100)

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert [kind for kind, _ in worker.bot.calls] == ["message", "photo"]  # type: ignore[attr-defined]
    text_message_id = message_ids[0]
    reply = worker.bot.calls[1][1]["reply_parameters"]  # type: ignore[attr-defined]
    assert reply.message_id == text_message_id
    assert worker.bot.calls[1][1]["caption"] is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_short_album_keeps_full_text_in_first_media_caption(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = AuditBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Короткий текст", media_count=4)

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert len(message_ids) == 4
    assert [kind for kind, _ in worker.bot.calls] == ["media_group"]  # type: ignore[attr-defined]
    media = worker.bot.calls[0][1]["media"]  # type: ignore[attr-defined]
    assert "Короткий текст" in media[0].caption
    assert all(row.caption is None for row in media[1:])


def test_observed_remote_id_is_not_reported_as_a_gap() -> None:
    assert not _remote_is_missing(
        first_run=False,
        remote_id=18348,
        state_id=18347,
        stored_id=18347,
        stored_ids={18347},
        remote_observed=True,
    )
    assert _remote_is_missing(
        first_run=False,
        remote_id=18348,
        state_id=18347,
        stored_id=18347,
        stored_ids={18347},
        remote_observed=False,
    )


def test_type_not_found_error_never_serializes_binary_payload() -> None:
    class TypeNotFoundError(Exception):
        def __str__(self) -> str:
            raise AssertionError("binary exception payload must not be rendered")

    message = _safe_exception_text(TypeNotFoundError())

    assert message == "TypeNotFoundError: Telegram payload decode failed; client restart required"


def test_integrity_observation_migration_resets_false_positive_counters() -> None:
    migration = Path("alembic/versions/0007_integrity_observation.py").read_text(encoding="utf-8")
    assert len("0007_integrity_observation") <= 32
    assert 'revision = "0007_integrity_observation"' in migration
    assert 'down_revision = "0006_integrity_counter_guard"' in migration
    assert "consecutive_gaps = 0" in migration


@pytest.mark.asyncio
async def test_compact_media_failure_still_sends_complete_text_once(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = FailingMediaBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Короткий текст")

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert len(message_ids) == 1
    assert [kind for kind, _ in worker.bot.calls] == ["photo", "message"]  # type: ignore[attr-defined]
    fallback_text = worker.bot.calls[1][1]["text"]  # type: ignore[attr-defined]
    assert fallback_text.count("Короткий текст") == 1
    assert "Вложения не удалось приложить" in fallback_text


@pytest.mark.asyncio
async def test_long_media_failure_does_not_repeat_original_text(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = FailingMediaBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Д" * 1100)

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert len(message_ids) == 2
    assert [kind for kind, _ in worker.bot.calls] == ["message", "photo", "message"]  # type: ignore[attr-defined]
    original_text = worker.bot.calls[0][1]["text"]  # type: ignore[attr-defined]
    warning_text = worker.bot.calls[2][1]["text"]  # type: ignore[attr-defined]
    assert "Д" * 100 in original_text
    assert "Д" * 100 not in warning_text
    assert "Вложения не удалось приложить" in warning_text
    assert worker.bot.calls[2][1]["reply_parameters"].message_id == message_ids[0]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_partial_compact_album_failure_preserves_actual_message_order(tmp_path: Path) -> None:
    worker = object.__new__(DeliveryWorker)
    worker.bot = FailingMediaBot()  # type: ignore[assignment]
    delivery = _delivery(tmp_path, "Короткий текст", media_count=11)

    message_ids = await worker._send(delivery)  # type: ignore[arg-type]

    assert [kind for kind, _ in worker.bot.calls] == ["media_group", "photo", "message"]  # type: ignore[attr-defined]
    assert len(message_ids) == 11
    first_album_ids = list(range(101, 111))
    assert message_ids[:10] == first_album_ids
    assert message_ids[-1] == 111
    warning = worker.bot.calls[-1][1]  # type: ignore[attr-defined]
    assert "Часть вложений не удалось приложить" in warning["text"]
    assert warning["reply_parameters"].message_id == first_album_ids[0]
