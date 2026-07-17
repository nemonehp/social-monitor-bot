import pytest

from app.db.enums import Platform
from app.utils.links import normalize_source_link, normalize_tg_link, normalize_vk_link


def test_tg_formats():
    assert normalize_tg_link("@example_name").normalized_link == "https://t.me/example_name"
    assert normalize_tg_link("https://t.me/s/example_name/123").identifier == "example_name"


def test_vk_formats():
    assert normalize_vk_link("vk.com/club123").identifier == "club123"
    assert normalize_vk_link("https://vk.com/wall-123_456").kind == "wall_post"
    assert normalize_vk_link("https://vk.com/id42").platform == Platform.VK


def test_reject_private_tg_invite():
    with pytest.raises(ValueError):
        normalize_source_link("https://t.me/+abcdef")
