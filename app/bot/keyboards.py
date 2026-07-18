from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [("Добавить источник", "source:add")],
        [("Источники", "source:menu")],
    ]
    if admin:
        rows.append([("Управление", "admin:menu")])
    return kb(rows)


def add_source_menu() -> InlineKeyboardMarkup:
    return kb([
        [("Одна ссылка", "source:add:one")],
        [("Загрузить файл", "source:add:file")],
        [("Назад", "menu:main")],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [("Частота проверки", "admin:interval")],
        [("Сигнальный чат", "admin:signal")],
        [("Прокси VK", "admin:proxies")],
        [("Аккаунты", "admin:accounts")],
        [("Доступ", "admin:access")],
        [("Состояние системы", "admin:status")],
        [("Назад", "menu:main")],
    ])


def proxy_menu() -> InlineKeyboardMarkup:
    return kb([
        [("Добавить прокси", "admin:proxy:add")],
        [("Состояние пула", "admin:proxy:status")],
        [("Назад", "admin:menu")],
    ])


def accounts_menu() -> InlineKeyboardMarkup:
    return kb([
        [("Добавить VK-токены", "admin:accounts:vk")],
        [("Добавить TG-сессии", "admin:accounts:tg")],
        [("Состояние", "admin:accounts:status")],
        [("Назад", "admin:menu")],
    ])


def access_menu() -> InlineKeyboardMarkup:
    return kb([
        [("Добавить пользователя", "admin:access:add")],
        [("Список пользователей", "admin:access:list")],
        [("Назад", "admin:menu")],
    ])
