from __future__ import annotations

import hashlib
import io
import math
import secrets
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.bot.keyboards import (
    access_menu,
    accounts_menu,
    add_source_menu,
    admin_menu,
    kb,
    main_menu,
    proxy_menu,
)
from app.bot.states import AddSourceState, AdminState, EditRegionState, SearchState
from app.config import Settings
from app.db.enums import DeliveryStatus, JobStatus, Platform, SourceStatus
from app.db.models import CollectionJob, Delivery, Source
from app.db.repositories import (
    AccessRepository,
    AuditRepository,
    CredentialRepository,
    ProxyRepository,
    SettingsRepository,
    SourceRepository,
)
from app.db.session import SessionFactory
from app.services.account_importer import parse_tg_accounts, parse_vk_accounts
from app.services.credential_manager import CredentialManager
from app.services.importer import errors_csv, parse_source_file
from app.services.proxy_manager import ProxyManager
from app.utils.links import normalize_source_link
from app.utils.regions import federal_district_for, normalize_region
from app.utils.text import h

router = Router(name="main")


async def edit_or_answer(event: CallbackQuery, text: str, markup=None) -> None:
    if event.message:
        try:
            await event.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
        await event.message.answer(text, reply_markup=markup)


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id == settings.admin_telegram_id


async def clear_state_files(state: FSMContext) -> None:
    data = await state.get_data()
    preview_path = data.get("preview_path")
    if preview_path:
        path = Path(preview_path)
        try:
            for child in path.parent.iterdir():
                child.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass
    await state.clear()


async def show_main(event: Message | CallbackQuery, settings: Settings) -> None:
    user_id = event.from_user.id
    text = "<b>Мониторинг источников</b>\n\nОбщий пул VK и Telegram."
    markup = main_menu(is_admin(user_id, settings))
    if isinstance(event, CallbackQuery):
        await edit_or_answer(event, text, markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, settings: Settings) -> None:
    await clear_state_files(state)
    args = (message.text or "").split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""
    if payload.startswith("bind_") and message.chat.type in {"group", "supergroup", "channel"}:
        if not is_admin(message.from_user.id, settings):
            await message.answer("Подключить сигнальный чат может только администратор.")
            return
        token = payload.removeprefix("bind_")
        async with SessionFactory() as session:
            async with session.begin():
                expected = await SettingsRepository.get(session, "signal_bind_token", "")
                if not expected or token != expected:
                    await message.answer("Ссылка подключения устарела. Создайте новую в управлении.")
                    return
                await SettingsRepository.set(session, "signal_chat_id", message.chat.id)
                await SettingsRepository.set(session, "signal_bind_token", "")
                await AuditRepository.write(
                    session,
                    message.from_user.id,
                    "signal_chat_bound",
                    "chat",
                    str(message.chat.id),
                )
        await message.answer("✅ Этот чат подключён для сигналов.")
        return
    await show_main(message, settings)


@router.callback_query(F.data == "menu:main")
async def menu_main(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await clear_state_files(state)
    await show_main(callback, settings)


@router.callback_query(F.data == "source:add")
async def source_add_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await clear_state_files(state)
    await edit_or_answer(callback, "<b>Добавление источника</b>", add_source_menu())
    await callback.answer()


@router.callback_query(F.data == "source:add:one")
async def source_add_one(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceState.waiting_link)
    await edit_or_answer(
        callback,
        "Отправьте ссылку VK или Telegram.",
        kb([[("Отмена", "source:add")]]),
    )
    await callback.answer()


@router.message(AddSourceState.waiting_link, F.text)
async def source_receive_link(message: Message, state: FSMContext) -> None:
    try:
        normalized = normalize_source_link(message.text or "")
    except ValueError as exc:
        await message.answer(
            f"Не удалось принять ссылку: {h(exc)}",
            reply_markup=kb([[("Отмена", "source:add")]]),
        )
        return
    await state.update_data(
        platform=normalized.platform.value,
        input_link=normalized.input_link,
        normalized_link=normalized.normalized_link,
        identifier=normalized.identifier,
    )
    await state.set_state(AddSourceState.waiting_region)
    await message.answer(
        "Введите регион. Федеральный округ бот определит автоматически.",
        reply_markup=kb([[("Без региона", "source:region:skip")], [("Отмена", "source:add")]]),
    )


async def _source_confirm(message: Message, state: FSMContext, region: str) -> None:
    data = await state.get_data()
    region = normalize_region(region)
    district = federal_district_for(region)
    await state.update_data(region=region, federal_district=district)
    await state.set_state(AddSourceState.confirm)
    platform = "VK" if data["platform"] == "vk" else "Telegram"
    location = " · ".join(x for x in [region, district] if x) or "Без региона"
    await message.answer(
        f"<b>Добавить источник?</b>\n\n{platform}\n{h(data['normalized_link'])}\n{h(location)}",
        reply_markup=kb([
            [("Добавить", "source:confirm:add")],
            [("Отмена", "source:add")],
        ]),
    )


@router.message(AddSourceState.waiting_region, F.text)
async def source_receive_region(message: Message, state: FSMContext) -> None:
    await _source_confirm(message, state, message.text or "")


@router.callback_query(F.data == "source:region:skip")
async def source_skip_region(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message:
        await _source_confirm(callback.message, state, "")
    await callback.answer()


@router.callback_query(AddSourceState.confirm, F.data == "source:confirm:add")
async def source_confirm_add(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    async with SessionFactory() as session:
        async with session.begin():
            source, created = await SourceRepository.add(
                session,
                platform=Platform(data["platform"]),
                input_link=data["input_link"],
                normalized_link=data["normalized_link"],
                external_id=data.get("identifier", ""),
                region=data.get("region", ""),
                federal_district=data.get("federal_district", ""),
                added_by=callback.from_user.id,
            )
            await AuditRepository.write(
                session,
                callback.from_user.id,
                "source_added" if created else "source_updated",
                "source",
                str(source.id),
            )
    await state.clear()
    await edit_or_answer(
        callback,
        "✅ Источник добавлен." if created else "Источник уже был в пуле; данные обновлены.",
        main_menu(is_admin(callback.from_user.id, settings)),
    )
    await callback.answer()


@router.callback_query(F.data == "source:add:file")
async def source_add_file(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceState.waiting_file)
    await edit_or_answer(
        callback,
        "Отправьте XLSX, CSV, TSV или TXT.\n\nПоддерживается ваша таблица с отдельными колонками TG и VK.",
        kb([[("Отмена", "source:add")]]),
    )
    await callback.answer()


@router.message(AddSourceState.waiting_file, F.document)
async def source_receive_file(message: Message, state: FSMContext, bot: Bot) -> None:
    suffix = Path(message.document.file_name or "upload").suffix.lower()
    if suffix not in {".xlsx", ".csv", ".tsv", ".txt"}:
        await message.answer("Поддерживаются XLSX, CSV, TSV и TXT.")
        return
    temp_dir = Path(tempfile.mkdtemp(prefix="social-import-"))
    path = temp_dir / f"input{suffix}"
    await bot.download(message.document, destination=path)
    try:
        preview = parse_source_file(path)
    except Exception as exc:
        await message.answer(f"Ошибка чтения файла: {h(exc)}")
        return
    preview_path = temp_dir / "preview.json"
    preview_path.write_bytes(
        orjson.dumps({
            "input_rows": preview.input_rows,
            "candidates": [
                {**asdict(c), "platform": c.platform.value} for c in preview.candidates
            ],
            "errors": [asdict(e) for e in preview.errors],
        })
    )
    await state.update_data(preview_path=str(preview_path))
    await state.set_state(AddSourceState.confirm_file)
    if preview.errors:
        await message.answer_document(
            BufferedInputFile(errors_csv(preview), filename="import_errors.csv"),
            caption="Строки, которые не удалось принять.",
        )
    await message.answer(
        "<b>Проверка файла завершена</b>\n\n"
        f"Строк: {preview.input_rows}\n"
        f"Источников к обработке: {len(preview.candidates)}\n"
        f"Ошибок: {len(preview.errors)}",
        reply_markup=kb([
            [("Импортировать", "source:file:confirm")],
            [("Отмена", "source:add")],
        ]),
    )


@router.callback_query(AddSourceState.confirm_file, F.data == "source:file:confirm")
async def source_file_confirm(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    preview_path = Path(data["preview_path"])
    payload = orjson.loads(preview_path.read_bytes())
    created = updated = 0
    async with SessionFactory() as session:
        async with session.begin():
            interval = int(await SettingsRepository.get(session, "poll_interval_seconds", settings.default_poll_interval_seconds))
            candidates = payload["candidates"]
            bulk_rows = []
            now = datetime.now(UTC)
            for index, row in enumerate(candidates):
                stagger = int(index * interval / max(1, len(candidates)))
                bulk_rows.append({
                    **row,
                    "metadata": {"import_row": row.get("row_number", 0)},
                    "next_check_at": now + timedelta(seconds=stagger),
                })
            created, updated = await SourceRepository.bulk_add(
                session,
                bulk_rows,
                added_by=callback.from_user.id,
            )
            await AuditRepository.write(
                session,
                callback.from_user.id,
                "sources_imported",
                payload={"created": created, "updated": updated},
            )
    try:
        for child in preview_path.parent.iterdir():
            child.unlink(missing_ok=True)
        preview_path.parent.rmdir()
    except OSError:
        pass
    await state.clear()
    await edit_or_answer(
        callback,
        f"✅ Импорт завершён.\n\nДобавлено: {created}\nУже существовало/обновлено: {updated}",
        main_menu(is_admin(callback.from_user.id, settings)),
    )
    await callback.answer()


SOURCE_PAGE_SIZE = 8


def _location_token(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _display_location(value: str, empty: str) -> str:
    return value or empty


def _source_return_callback(code: str) -> str:
    parts = code.split(".")
    if len(parts) == 2 and parts[0] in {"av", "at"}:
        platform = "vk" if parts[0] == "av" else "telegram"
        return f"source:alpha:{platform}:{parts[1]}"
    if len(parts) == 4 and parts[0] == "r":
        return f"source:regionitems:{parts[1]}:{parts[2]}:{parts[3]}"
    if len(parts) == 2 and parts[0] == "s":
        return f"source:search:page:{parts[1]}"
    return "source:menu"


async def _resolve_district(session, token: str) -> str | None:
    for district, _count in await SourceRepository.district_counts(session):
        if _location_token(district) == token:
            return district
    return None


async def _resolve_region(session, district: str, token: str) -> str | None:
    for region, _count in await SourceRepository.region_counts(session, district):
        if _location_token(region) == token:
            return region
    return None


def _source_button_text(source: Source) -> str:
    title = source.title or source.normalized_link.rstrip("/").split("/")[-1]
    return title[:42]


async def _source_list_payload(
    *,
    page: int,
    query: str = "",
    platform: Platform | None = None,
    region: str | None = None,
    federal_district: str | None = None,
    alphabetical: bool = True,
    return_code_prefix: str,
    callback_prefix: str,
) -> tuple[str, InlineKeyboardMarkup]:
    requested_page = max(1, page)
    async with SessionFactory() as session:
        sources, total = await SourceRepository.list(
            session,
            page=requested_page,
            page_size=SOURCE_PAGE_SIZE,
            query=query,
            platform=platform,
            region=region,
            federal_district=federal_district,
            alphabetical=alphabetical,
        )
        pages = max(1, math.ceil(total / SOURCE_PAGE_SIZE))
        page = min(requested_page, pages)
        if page != requested_page:
            sources, _total = await SourceRepository.list(
                session,
                page=page,
                page_size=SOURCE_PAGE_SIZE,
                query=query,
                platform=platform,
                region=region,
                federal_district=federal_district,
                alphabetical=alphabetical,
            )
    rows: list[list[tuple[str, str]]] = []
    for source in sources:
        code = return_code_prefix.format(page=page)
        icon = "VK" if source.platform == Platform.VK else "TG"
        rows.append([(
            f"{icon} · {_source_button_text(source)}",
            f"source:view:{source.id}:{code}",
        )])
    nav: list[tuple[str, str]] = []
    if page > 1:
        nav.append(("←", callback_prefix.format(page=page - 1)))
    nav.append((f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(("→", callback_prefix.format(page=page + 1)))
    rows.append(nav)
    rows.append([("К разделам", "source:menu")])
    return f"Найдено: {total}", kb(rows)


@router.callback_query(F.data == "source:menu")
async def source_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    async with SessionFactory() as session:
        vk_count = int(await session.scalar(
            select(func.count()).select_from(Source).where(
                Source.status != SourceStatus.DELETED,
                Source.platform == Platform.VK,
            )
        ) or 0)
        tg_count = int(await session.scalar(
            select(func.count()).select_from(Source).where(
                Source.status != SourceStatus.DELETED,
                Source.platform == Platform.TELEGRAM,
            )
        ) or 0)
    await edit_or_answer(
        callback,
        f"<b>Источники</b>\n\nVK: {vk_count}\nTelegram: {tg_count}",
        kb([
            [("🔎 Поиск", "source:search")],
            [("🗺 По ФО и регионам", "source:districts:1")],
            [(f"VK · алфавит ({vk_count})", "source:alpha:vk:1")],
            [(f"Telegram · алфавит ({tg_count})", "source:alpha:telegram:1")],
            [("Назад", "menu:main")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("source:list:"))
async def source_list_legacy(callback: CallbackQuery, state: FSMContext) -> None:
    """Keep buttons from messages sent by version 1.0 usable after upgrade."""
    callback.data = "source:menu"
    await source_menu(callback, state)


@router.callback_query(F.data.startswith("source:alpha:"))
async def source_alpha(callback: CallbackQuery) -> None:
    _, _, platform_value, page_value = callback.data.split(":")
    platform = Platform(platform_value)
    page = int(page_value)
    short = "av" if platform == Platform.VK else "at"
    count_text, markup = await _source_list_payload(
        page=page,
        platform=platform,
        return_code_prefix=f"{short}.{{page}}",
        callback_prefix=f"source:alpha:{platform.value}:{{page}}",
    )
    name = "VK" if platform == Platform.VK else "Telegram"
    await edit_or_answer(callback, f"<b>{name} · по алфавиту</b>\n\n{count_text}", markup)
    await callback.answer()


@router.callback_query(F.data.startswith("source:districts:"))
async def source_districts(callback: CallbackQuery) -> None:
    page = int(callback.data.rsplit(":", 1)[-1])
    async with SessionFactory() as session:
        districts = await SourceRepository.district_counts(session)
    pages = max(1, math.ceil(len(districts) / SOURCE_PAGE_SIZE))
    page = min(max(1, page), pages)
    chunk = districts[(page - 1) * SOURCE_PAGE_SIZE: page * SOURCE_PAGE_SIZE]
    rows = [[
        (
            f"{_display_location(district, 'Без округа')} · {count}",
            f"source:regions:{_location_token(district)}:1",
        )
    ] for district, count in chunk]
    nav = []
    if page > 1:
        nav.append(("←", f"source:districts:{page - 1}"))
    nav.append((f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(("→", f"source:districts:{page + 1}"))
    rows.append(nav)
    rows.append([("К разделам", "source:menu")])
    await edit_or_answer(callback, "<b>Федеральные округа</b>", kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("source:regions:"))
async def source_regions(callback: CallbackQuery) -> None:
    _, _, district_token, page_value = callback.data.split(":")
    page = int(page_value)
    async with SessionFactory() as session:
        district = await _resolve_district(session, district_token)
        if district is None:
            await callback.answer("Список изменился, откройте раздел заново", show_alert=True)
            return
        regions = await SourceRepository.region_counts(session, district)
    pages = max(1, math.ceil(len(regions) / SOURCE_PAGE_SIZE))
    page = min(max(1, page), pages)
    chunk = regions[(page - 1) * SOURCE_PAGE_SIZE: page * SOURCE_PAGE_SIZE]
    rows = [[
        (
            f"{_display_location(region, 'Без региона')} · {count}",
            f"source:regionitems:{district_token}:{_location_token(region)}:1",
        )
    ] for region, count in chunk]
    nav = []
    if page > 1:
        nav.append(("←", f"source:regions:{district_token}:{page - 1}"))
    nav.append((f"{page}/{pages}", "noop"))
    if page < pages:
        nav.append(("→", f"source:regions:{district_token}:{page + 1}"))
    rows.append(nav)
    rows.append([("К округам", "source:districts:1")])
    await edit_or_answer(
        callback,
        f"<b>{h(_display_location(district, 'Без округа'))}</b>\n\nВыберите регион.",
        kb(rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("source:regionitems:"))
async def source_region_items(callback: CallbackQuery) -> None:
    _, _, district_token, region_token, page_value = callback.data.split(":")
    page = int(page_value)
    async with SessionFactory() as session:
        district = await _resolve_district(session, district_token)
        if district is None:
            await callback.answer("Округ не найден", show_alert=True)
            return
        region = await _resolve_region(session, district, region_token)
        if region is None:
            await callback.answer("Регион не найден", show_alert=True)
            return
    count_text, markup = await _source_list_payload(
        page=page,
        region=region,
        federal_district=district,
        return_code_prefix=f"r.{district_token}.{region_token}.{{page}}",
        callback_prefix=f"source:regionitems:{district_token}:{region_token}:{{page}}",
    )
    # Replace the generic section button with a region-level back button.
    markup.inline_keyboard[-1] = [InlineKeyboardButton(
        text="К регионам",
        callback_data=f"source:regions:{district_token}:1",
    )]
    await edit_or_answer(
        callback,
        f"<b>{h(_display_location(region, 'Без региона'))}</b>\n"
        f"{h(_display_location(district, 'Без округа'))}\n\n{count_text}",
        markup,
    )
    await callback.answer()


@router.callback_query(F.data == "source:search")
async def source_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchState.waiting_query)
    await edit_or_answer(
        callback,
        "Введите ID, название, полную ссылку или часть ссылки.",
        kb([[("Отмена", "source:menu")]]),
    )
    await callback.answer()


async def _render_search_message(message: Message, state: FSMContext, page: int) -> None:
    data = await state.get_data()
    query = str(data.get("source_search_query") or "").strip()
    if not query:
        await message.answer("Поисковый запрос потерян. Откройте поиск заново.")
        return
    count_text, markup = await _source_list_payload(
        page=page,
        query=query,
        return_code_prefix="s.{page}",
        callback_prefix="source:search:page:{page}",
    )
    await message.answer(
        f"<b>Поиск</b>\nЗапрос: <code>{h(query)}</code>\n\n{count_text}",
        reply_markup=markup,
    )


@router.message(SearchState.waiting_query, F.text)
async def source_search_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer("Введите непустой запрос.")
        return
    await state.update_data(source_search_query=query)
    await state.set_state(None)
    await _render_search_message(message, state, 1)


@router.callback_query(F.data.startswith("source:search:page:"))
async def source_search_page(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    query = str(data.get("source_search_query") or "").strip()
    if not query:
        await callback.answer("Запрос устарел. Запустите поиск заново.", show_alert=True)
        return
    count_text, markup = await _source_list_payload(
        page=page,
        query=query,
        return_code_prefix="s.{page}",
        callback_prefix="source:search:page:{page}",
    )
    await edit_or_answer(
        callback,
        f"<b>Поиск</b>\nЗапрос: <code>{h(query)}</code>\n\n{count_text}",
        markup,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("source:view:"))
async def source_view(callback: CallbackQuery) -> None:
    _, _, source_id, return_code = callback.data.split(":", 3)
    async with SessionFactory() as session:
        source = await SourceRepository.get(session, int(source_id))
    if not source:
        await callback.answer("Источник не найден", show_alert=True)
        return
    status = {
        SourceStatus.ACTIVE: "работает",
        SourceStatus.PAUSED: "приостановлен",
        SourceStatus.ERROR: "ошибка",
        SourceStatus.DELETED: "удалён",
    }[source.status]
    location = " · ".join(x for x in [source.region, source.federal_district] if x) or "Без региона"
    text = (
        f"<b>{h(source.title or source.normalized_link)}</b>\n"
        f"ID: <code>{source.id}</code>\n"
        f"Внешний ID: <code>{h(source.external_id or 'не определён')}</code>\n"
        f"{source.platform.value.upper()}\n"
        f"{h(location)}\n\n"
        f"{h(source.normalized_link)}\n"
        f"Статус: {status}\n"
        f"Последняя успешная проверка: {source.last_success_at or 'ещё не было'}"
    )
    toggle = (
        ("Приостановить", f"source:pause:{source.id}:{return_code}")
        if source.status == SourceStatus.ACTIVE
        else ("Возобновить", f"source:resume:{source.id}:{return_code}")
    )
    await edit_or_answer(callback, text, kb([
        [toggle],
        [("Изменить регион", f"source:region:{source.id}")],
        [("Удалить", f"source:delete:ask:{source.id}:{return_code}")],
        [("Назад", _source_return_callback(return_code))],
    ]))
    await callback.answer()


@router.callback_query(F.data.startswith("source:pause:"))
async def source_pause(callback: CallbackQuery) -> None:
    _, _, source_id, return_code = callback.data.split(":", 3)
    async with SessionFactory() as session:
        async with session.begin():
            await SourceRepository.set_status(session, int(source_id), SourceStatus.PAUSED)
            await AuditRepository.write(session, callback.from_user.id, "source_paused", "source", source_id)
    callback.data = f"source:view:{source_id}:{return_code}"
    await source_view(callback)


@router.callback_query(F.data.startswith("source:resume:"))
async def source_resume(callback: CallbackQuery) -> None:
    _, _, source_id, return_code = callback.data.split(":", 3)
    async with SessionFactory() as session:
        async with session.begin():
            await SourceRepository.set_status(session, int(source_id), SourceStatus.ACTIVE)
            await AuditRepository.write(session, callback.from_user.id, "source_resumed", "source", source_id)
    callback.data = f"source:view:{source_id}:{return_code}"
    await source_view(callback)


@router.callback_query(F.data.startswith("source:delete:ask:"))
async def source_delete_ask(callback: CallbackQuery) -> None:
    _, _, _, source_id, return_code = callback.data.split(":", 4)
    await edit_or_answer(
        callback,
        "Удалить источник из общего пула? История дедупликации сохранится.",
        kb([
            [("Удалить", f"source:delete:yes:{source_id}:{return_code}")],
            [("Отмена", f"source:view:{source_id}:{return_code}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("source:delete:yes:"))
async def source_delete_yes(callback: CallbackQuery) -> None:
    _, _, _, source_id, return_code = callback.data.split(":", 4)
    async with SessionFactory() as session:
        async with session.begin():
            await SourceRepository.set_status(session, int(source_id), SourceStatus.DELETED)
            await AuditRepository.write(session, callback.from_user.id, "source_deleted", "source", source_id)
    await edit_or_answer(
        callback,
        "✅ Источник удалён.",
        kb([[("Вернуться", _source_return_callback(return_code))]]),
    )
    await callback.answer("Удалено")


@router.callback_query(F.data.startswith("source:region:"))
async def source_region_edit(callback: CallbackQuery, state: FSMContext) -> None:
    source_id = int(callback.data.rsplit(":", 1)[-1])
    await state.set_state(EditRegionState.waiting_region)
    await state.update_data(source_id=source_id)
    await edit_or_answer(callback, "Введите новый регион.", kb([[("Отмена", f"source:view:{source_id}:m")]]))
    await callback.answer()


@router.message(EditRegionState.waiting_region, F.text)
async def source_region_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    region = normalize_region(message.text or "")
    district = federal_district_for(region)
    async with SessionFactory() as session:
        async with session.begin():
            await SourceRepository.update_region(session, int(data["source_id"]), region, district)
            await AuditRepository.write(
                session,
                message.from_user.id,
                "source_region_changed",
                "source",
                str(data["source_id"]),
                {"region": region, "district": district},
            )
    await state.clear()
    await message.answer(f"✅ Регион обновлён: {h(region)} · {h(district or 'округ не определён')}")


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------------- Admin ----------------

@router.callback_query(F.data == "admin:menu")
async def admin_open(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("Только для администратора", show_alert=True)
        return
    await edit_or_answer(callback, "<b>Управление</b>", admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:interval")
async def admin_interval(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings):
        return
    async with SessionFactory() as session:
        current = await SettingsRepository.get(session, "poll_interval_seconds", settings.default_poll_interval_seconds)
    await edit_or_answer(callback, f"Текущий интервал: <b>{current} сек.</b>", kb([
        [("1 мин", "admin:interval:set:60"), ("2 мин", "admin:interval:set:120")],
        [("5 мин", "admin:interval:set:300"), ("10 мин", "admin:interval:set:600")],
        [("Другое значение", "admin:interval:custom")],
        [("Назад", "admin:menu")],
    ]))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:interval:set:"))
async def admin_interval_set(callback: CallbackQuery, settings: Settings) -> None:
    value = int(callback.data.rsplit(":", 1)[-1])
    if value < settings.min_poll_interval_seconds:
        await callback.answer(f"Минимум {settings.min_poll_interval_seconds} секунд", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            await SettingsRepository.set(session, "poll_interval_seconds", value)
            await AuditRepository.write(session, callback.from_user.id, "poll_interval_changed", payload={"seconds": value})
    await callback.answer("Сохранено")
    callback.data = "admin:interval"
    await admin_interval(callback, settings)


@router.callback_query(F.data == "admin:interval:custom")
async def admin_interval_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_interval)
    await edit_or_answer(callback, "Введите интервал в секундах.", kb([[("Отмена", "admin:interval")]]))
    await callback.answer()


@router.message(AdminState.waiting_interval, F.text)
async def admin_interval_custom_save(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        value = int(message.text or "")
        if value < settings.min_poll_interval_seconds:
            raise ValueError
    except ValueError:
        await message.answer(f"Введите целое число не меньше {settings.min_poll_interval_seconds}.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            await SettingsRepository.set(session, "poll_interval_seconds", value)
    await state.clear()
    await message.answer(f"✅ Интервал: {value} сек.")


@router.callback_query(F.data == "admin:signal")
async def admin_signal(callback: CallbackQuery, bot: Bot, settings: Settings) -> None:
    token = secrets.token_urlsafe(12)
    me = await bot.get_me()
    async with SessionFactory() as session:
        async with session.begin():
            current = await SettingsRepository.get(session, "signal_chat_id", None)
            await SettingsRepository.set(session, "signal_bind_token", token)
    url = f"https://t.me/{me.username}?startgroup=bind_{token}"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подключить чат", url=url)],
        [InlineKeyboardButton(text="Назад", callback_data="admin:menu")],
    ])
    await edit_or_answer(callback, f"Сейчас подключён: <code>{current or 'нет'}</code>\n\nДобавьте бота в нужную группу этой кнопкой.", markup)
    await callback.answer()


@router.callback_query(F.data == "admin:proxies")
async def admin_proxies(callback: CallbackQuery) -> None:
    await edit_or_answer(callback, "<b>Прокси VK</b>", proxy_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:proxy:add")
async def admin_proxy_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_proxy_input)
    await edit_or_answer(callback, "Отправьте прокси строками или TXT-файлом. Поддерживаются HTTP(S), SOCKS4/5 и форматы host:port[:user:pass].", kb([[("Отмена", "admin:proxies")]]))
    await callback.answer()


async def _proxy_input_text(message: Message, text: str, state: FSMContext, settings: Settings) -> None:
    progress = await message.answer("Проверяю маршруты, страну IP и доступ к VK…")
    manager = ProxyManager(settings)
    results = await manager.check_many(text)
    created, updated = await manager.save_working(results)
    async with SessionFactory() as session:
        async with session.begin():
            await AuditRepository.write(
                session,
                message.from_user.id,
                "vk_proxies_imported",
                payload={"created": created, "updated": updated, "failed": len([r for r in results if not r.ok])},
            )
    failed = [r for r in results if not r.ok]
    details = "\n".join(f"• {h(r.raw[:60])}: {h(r.reason)}" for r in failed[:10])
    await progress.edit_text(
        "<b>Проверка прокси завершена</b>\n\n"
        f"Рабочих RU: {created + updated}\n"
        f"Добавлено: {created}\n"
        f"Обновлено: {updated}\n"
        f"Отклонено: {len(failed)}"
        + (f"\n\n{details}" if details else ""),
        reply_markup=proxy_menu(),
    )
    await state.clear()


@router.message(AdminState.waiting_proxy_input, F.text)
async def admin_proxy_text(message: Message, state: FSMContext, settings: Settings) -> None:
    await _proxy_input_text(message, message.text or "", state, settings)


@router.message(AdminState.waiting_proxy_input, F.document)
async def admin_proxy_file(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    buffer = io.BytesIO()
    await bot.download(message.document, destination=buffer)
    text = buffer.getvalue().decode("utf-8-sig", errors="replace")
    await _proxy_input_text(message, text, state, settings)


@router.callback_query(F.data == "admin:proxy:status")
async def admin_proxy_status(callback: CallbackQuery) -> None:
    async with SessionFactory() as session:
        counts = await ProxyRepository.counts(session)
    await edit_or_answer(callback, "<b>Пул VK-прокси</b>\n\n" + "\n".join(f"{h(k)}: {v}" for k, v in counts.items()), proxy_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:accounts")
async def admin_accounts(callback: CallbackQuery) -> None:
    await edit_or_answer(callback, "<b>Аккаунты</b>", accounts_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:accounts:vk")
async def admin_accounts_vk(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_vk_accounts)
    await edit_or_answer(callback, "Отправьте VK-токены по одному на строку. Допустимо: label;token", kb([[("Отмена", "admin:accounts")]]))
    await callback.answer()


@router.message(AdminState.waiting_vk_accounts, F.text)
async def admin_accounts_vk_save(message: Message, state: FSMContext, settings: Settings) -> None:
    accounts, errors = parse_vk_accounts(message.text or "")
    created, updated = await CredentialManager(settings.app_encryption_key).save_vk(accounts)
    async with SessionFactory() as session:
        async with session.begin():
            await AuditRepository.write(session, message.from_user.id, "vk_accounts_imported", payload={"created": created, "updated": updated, "errors": len(errors)})
    await state.clear()
    await message.answer(f"VK-токены: добавлено {created}, обновлено {updated}, ошибок {len(errors)}", reply_markup=accounts_menu())


@router.message(AdminState.waiting_vk_accounts, F.document)
async def admin_accounts_vk_file(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    buffer = io.BytesIO()
    await bot.download(message.document, destination=buffer)
    accounts, errors = parse_vk_accounts(buffer.getvalue().decode("utf-8-sig", errors="replace"))
    created, updated = await CredentialManager(settings.app_encryption_key).save_vk(accounts)
    async with SessionFactory() as session:
        async with session.begin():
            await AuditRepository.write(session, message.from_user.id, "vk_accounts_imported", payload={"created": created, "updated": updated, "errors": len(errors)})
    await state.clear()
    await message.answer(f"VK-токены: добавлено {created}, обновлено {updated}, ошибок {len(errors)}", reply_markup=accounts_menu())


@router.callback_query(F.data == "admin:accounts:tg")
async def admin_accounts_tg(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_tg_accounts)
    await edit_or_answer(callback, "Отправьте строки Telegram-аккаунтов в вашем существующем CSV-формате из 10 полей.", kb([[("Отмена", "admin:accounts")]]))
    await callback.answer()


@router.message(AdminState.waiting_tg_accounts, F.text)
async def admin_accounts_tg_save(message: Message, state: FSMContext, settings: Settings) -> None:
    accounts, errors = parse_tg_accounts(message.text or "")
    created, updated = await CredentialManager(settings.app_encryption_key).save_tg(accounts)
    async with SessionFactory() as session:
        async with session.begin():
            await AuditRepository.write(session, message.from_user.id, "tg_accounts_imported", payload={"created": created, "updated": updated, "errors": len(errors)})
    await state.clear()
    await message.answer(f"TG-сессии: добавлено {created}, обновлено {updated}, ошибок {len(errors)}", reply_markup=accounts_menu())


@router.message(AdminState.waiting_tg_accounts, F.document)
async def admin_accounts_tg_file(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    buffer = io.BytesIO()
    await bot.download(message.document, destination=buffer)
    accounts, errors = parse_tg_accounts(buffer.getvalue().decode("utf-8-sig", errors="replace"))
    created, updated = await CredentialManager(settings.app_encryption_key).save_tg(accounts)
    async with SessionFactory() as session:
        async with session.begin():
            await AuditRepository.write(session, message.from_user.id, "tg_accounts_imported", payload={"created": created, "updated": updated, "errors": len(errors)})
    await state.clear()
    await message.answer(f"TG-сессии: добавлено {created}, обновлено {updated}, ошибок {len(errors)}", reply_markup=accounts_menu())


@router.callback_query(F.data == "admin:accounts:status")
async def admin_accounts_status(callback: CallbackQuery) -> None:
    async with SessionFactory() as session:
        counts = await CredentialRepository.counts(session)
    await edit_or_answer(callback, "<b>Состояние аккаунтов</b>\n\n" + "\n".join(f"{h(k)}: {v}" for k, v in counts.items()), accounts_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:access")
async def admin_access(callback: CallbackQuery) -> None:
    await edit_or_answer(callback, "<b>Разрешённые пользователи</b>", access_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:access:add")
async def admin_access_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminState.waiting_user_id)
    await edit_or_answer(callback, "Введите Telegram user ID.", kb([[("Отмена", "admin:access")]]))
    await callback.answer()


@router.message(AdminState.waiting_user_id, F.text)
async def admin_access_save(message: Message, state: FSMContext, settings: Settings) -> None:
    try:
        user_id = int(message.text or "")
    except ValueError:
        await message.answer("Нужен числовой Telegram user ID.")
        return
    async with SessionFactory() as session:
        async with session.begin():
            await AccessRepository.add_user(session, user_id, "", settings.admin_telegram_id)
    await state.clear()
    await message.answer(f"✅ Пользователь {user_id} добавлен.", reply_markup=access_menu())


@router.callback_query(F.data == "admin:access:list")
async def admin_access_list(callback: CallbackQuery, settings: Settings) -> None:
    async with SessionFactory() as session:
        users = await AccessRepository.list_users(session)
    text = "<b>Разрешённые пользователи</b>\n\n" + "\n".join(
        f"• <code>{u.telegram_id}</code>{' · админ' if u.telegram_id == settings.admin_telegram_id else ''}{'' if u.active else ' · отключён'}"
        for u in users
    )
    rows = []
    for user in users:
        if user.active and user.telegram_id != settings.admin_telegram_id:
            rows.append([(f"Отключить {user.telegram_id}", f"admin:access:disable:{user.telegram_id}")])
    rows.append([("Назад", "admin:access")])
    await edit_or_answer(callback, text, kb(rows))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:access:disable:"))
async def admin_access_disable(callback: CallbackQuery, settings: Settings) -> None:
    user_id = int(callback.data.rsplit(":", 1)[-1])
    if user_id == settings.admin_telegram_id:
        await callback.answer("Администратора отключить нельзя", show_alert=True)
        return
    async with SessionFactory() as session:
        async with session.begin():
            await AccessRepository.disable_user(session, user_id)
            await AuditRepository.write(session, callback.from_user.id, "user_disabled", "user", str(user_id))
    callback.data = "admin:access:list"
    await admin_access_list(callback, settings)


@router.callback_query(F.data == "admin:status")
async def admin_status(callback: CallbackQuery) -> None:
    async with SessionFactory() as session:
        source_counts = dict((p.value, int(c)) for p, c in (await session.execute(select(Source.platform, func.count()).where(Source.status == SourceStatus.ACTIVE).group_by(Source.platform))).all())
        total_sources = int(await session.scalar(select(func.count()).select_from(Source).where(Source.status != SourceStatus.DELETED)) or 0)
        errors = int(await session.scalar(select(func.count()).select_from(Source).where(Source.status == SourceStatus.ERROR)) or 0)
        job_rows = (await session.execute(
            select(CollectionJob.platform, CollectionJob.status, func.count())
            .where(CollectionJob.status.in_([JobStatus.PENDING, JobStatus.RETRY, JobStatus.RUNNING]))
            .group_by(CollectionJob.platform, CollectionJob.status)
        )).all()
        job_counts = {(platform.value, status.value): int(count) for platform, status, count in job_rows}
        pending_jobs = sum(job_counts.values())
        pending_deliveries = int(await session.scalar(select(func.count()).select_from(Delivery).where(Delivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRY]))) or 0)
        proxy_counts = await ProxyRepository.counts(session)
        account_counts = await CredentialRepository.counts(session)
    def job_line(platform: str) -> str:
        pending = job_counts.get((platform, "pending"), 0)
        retry = job_counts.get((platform, "retry"), 0)
        running = job_counts.get((platform, "running"), 0)
        return f"ожидает {pending} · повтор {retry} · выполняется {running}"
    text = (
        "<b>Состояние системы</b>\n\n"
        f"Источники: {total_sources}\n"
        f"VK: {source_counts.get('vk', 0)}\n"
        f"Telegram: {source_counts.get('telegram', 0)}\n"
        f"С ошибкой: {errors}\n\n"
        f"Очередь проверок: {pending_jobs}\n"
        f"VK: {job_line('vk')}\n"
        f"Telegram: {job_line('telegram')}\n"
        f"Очередь доставок: {pending_deliveries}\n\n"
        f"Прокси: {h(proxy_counts)}\n"
        f"Аккаунты: {h(account_counts)}"
    )
    await edit_or_answer(callback, text, admin_menu())
    await callback.answer()
