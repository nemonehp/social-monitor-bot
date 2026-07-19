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
    for idx, row in enumerate(reader, start=1):
        fields = [part.strip() for part in row]
        if not fields or all(not part for part in fields):
            continue
        if len(fields) > 4 and (fields[2].lower() == "hash" or fields[3].lower() == "api_id"):
            continue
        if len(fields) < 10:
            errors.append(f"Строка {idx}: требуется минимум 10 CSV-полей")
            continue
        try:
            api_id = int(fields[3])
        except ValueError:
            errors.append(f"Строка {idx}: api_id не число")
            continue
        result.append(
            TgAccountInput(
                label=fields[0] or f"tg-{idx}",
                session=fields[2],
                api_id=api_id,
                api_hash=fields[4],
                device_model=fields[5],
                system_version=fields[6],
                app_version=fields[7],
                system_lang_code=fields[8],
                lang_code=fields[9],
            )
        )
    return result, errors
