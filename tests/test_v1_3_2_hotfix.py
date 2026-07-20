from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aiogram.enums import ParseMode
from aiogram.types import InputMediaPhoto

from app.services.delivery import DeliveryWorker


def test_media_group_caption_is_set_during_construction(tmp_path: Path) -> None:
    image = tmp_path / "preview.jpg"
    image.write_bytes(b"preview")
    row = SimpleNamespace(local_path=str(image), media_type="photo")

    media = DeliveryWorker._input_media(row, caption="<b>Карточка</b>")

    assert isinstance(media, InputMediaPhoto)
    assert media.caption == "<b>Карточка</b>"
    assert media.parse_mode == ParseMode.HTML


def test_delivery_never_mutates_frozen_media_models() -> None:
    source = Path("app/services/delivery.py").read_text(encoding="utf-8")
    assert "media_group[0].caption =" not in source
    assert "media_group[0].parse_mode =" not in source
    assert "caption=caption if index == 0 else None" in source


def test_unexpected_delivery_failures_use_bounded_backoff() -> None:
    source = Path("app/services/delivery.py").read_text(encoding="utf-8")
    assert "delay = min(900" in source
    assert "retry_in_seconds=delay" in source
