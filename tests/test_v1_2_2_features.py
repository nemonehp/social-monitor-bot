from __future__ import annotations

from pathlib import Path

import pytest

from app.bot import handlers
from app.bot.handlers import clear_state_files
from app.bot.keyboards import MAIN_MENU_TEXT, persistent_main_menu
from app.db.enums import Platform
from app.utils.platforms import platform_badge


def test_platform_badges_are_consistent_plain_unicode() -> None:
    assert platform_badge(Platform.TELEGRAM) == "🔵 TG"
    assert platform_badge(Platform.VK) == "🟢 VK"
    assert platform_badge("telegram") == "🔵 TG"
    assert platform_badge("vk") == "🟢 VK"


def test_persistent_main_menu_keyboard_has_single_reset_button() -> None:
    markup = persistent_main_menu()

    assert markup.is_persistent is True
    assert markup.resize_keyboard is True
    assert len(markup.keyboard) == 1
    assert len(markup.keyboard[0]) == 1
    assert markup.keyboard[0][0].text == MAIN_MENU_TEXT == "Главное меню"


@pytest.mark.asyncio
async def test_clear_state_files_removes_preview_directory_and_clears_state(tmp_path: Path) -> None:
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    preview_file = preview_dir / "preview.json"
    preview_file.write_text("{}", encoding="utf-8")

    class FakeState:
        cleared = False

        async def get_data(self) -> dict[str, str]:
            return {"preview_path": str(preview_file)}

        async def clear(self) -> None:
            self.cleared = True

    state = FakeState()
    await clear_state_files(state)  # type: ignore[arg-type]

    assert state.cleared is True
    assert not preview_dir.exists()


def test_release_sources_do_not_contain_legacy_platform_icons() -> None:
    for filename in [
        "app/services/delivery.py",
        "app/services/scheduler.py",
        "app/bot/handlers.py",
    ]:
        content = Path(filename).read_text(encoding="utf-8")
        assert "✈️" not in content
        assert "🟦" not in content


@pytest.mark.asyncio
async def test_main_menu_button_resets_state_before_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeState:
        async def get_data(self) -> dict[str, str]:
            return {}

        async def clear(self) -> None:
            events.append("cleared")

    async def fake_show_main(_message: object, _settings: object) -> None:
        events.append("rendered")

    monkeypatch.setattr(handlers, "show_main", fake_show_main)
    await handlers.main_menu_button(object(), FakeState(), object())  # type: ignore[arg-type]

    assert events == ["cleared", "rendered"]


def test_main_menu_reply_handler_is_registered_before_state_handlers() -> None:
    content = Path("app/bot/handlers.py").read_text(encoding="utf-8")
    main_handler = content.index("@router.message(F.text == MAIN_MENU_TEXT)")
    first_state_handler = content.index("@router.message(AddSourceState.waiting_link")
    assert main_handler < first_state_handler
