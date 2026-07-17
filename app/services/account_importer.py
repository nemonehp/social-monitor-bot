from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(slots=True)
class VkAccountInput:
    label: str
    token: str


@dataclass(slots=True)
class TgAccountInput:
    label: str
    session: str
    api_id: int
    api_hash: str
    device_model: str
    system_version: str
    app_version: str
    system_lang_code: str
    lang_code: str


def parse_vk_accounts(text: str) -> tuple[list[VkAccountInput], list[str]]:
    result: list[VkAccountInput] = []
    errors: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if ";" in value:
            label, token = [x.strip() for x in value.split(";", 1)]
        else:
            token = value
            label = f"vk-{idx}"
        if len(token) < 20:
            errors.append(f"Строка {idx}: токен выглядит слишком коротким")
            continue
        result.append(VkAccountInput(label=label or f"vk-{idx}", token=token))
    return result, errors


def parse_tg_accounts(text: str) -> tuple[list[TgAccountInput], list[str]]:
    result: list[TgAccountInput] = []
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))
    for idx, parts in enumerate(reader, start=1):
        parts = [p.strip() for p in parts]
        if not parts or all(not p for p in parts):
            continue
        if len(parts) > 4 and (parts[2].lower() == "hash" or parts[3].lower() == "api_id"):
            continue
        if len(parts) < 10:
            errors.append(f"Строка {idx}: требуется минимум 10 CSV-полей")
            continue
        try:
            api_id = int(parts[3])
        except ValueError:
            errors.append(f"Строка {idx}: api_id не число")
            continue
        result.append(
            TgAccountInput(
                label=parts[0] or f"tg-{idx}",
                session=parts[2],
                api_id=api_id,
                api_hash=parts[4],
                device_model=parts[5],
                system_version=parts[6],
                app_version=parts[7],
                system_lang_code=parts[8],
                lang_code=parts[9],
            )
        )
    return result, errors
