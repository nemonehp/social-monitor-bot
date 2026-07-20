from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse


@dataclass(slots=True)
class VkAccountInput:
    line_number: int
    token: str
    expires_at: datetime | None = None
    config: dict[str, str | int] = field(default_factory=dict)


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


def _vk_payload(value: str) -> tuple[str, datetime | None, dict[str, str | int]]:
    payload = value.strip()
    data: dict[str, object] = {}
    if payload.startswith("{"):
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("JSON должен быть объектом")
        data = parsed
    elif "access_token=" in payload:
        parsed_url = urlparse(payload if "://" in payload else f"https://local/?{payload.lstrip('#?')}")
        query = parse_qs(parsed_url.query)
        fragment = parse_qs(parsed_url.fragment)
        merged = {**query, **fragment}
        data = {key: values[-1] for key, values in merged.items() if values}
    else:
        return payload, None, {}

    token = str(data.get("access_token") or data.get("token") or "").strip()
    if not token:
        raise ValueError("не найден access_token")
    config: dict[str, str | int] = {}
    for key in ("user_id", "app_id", "scope"):
        value_obj = data.get(key)
        if value_obj not in (None, ""):
            value_text = str(value_obj)
            config[key] = (
                int(value_text) if key in {"user_id", "app_id"} and value_text.isdigit() else value_text
            )
    expires_at: datetime | None = None
    expires_in = data.get("expires_in")
    if expires_in not in (None, "", "0", 0):
        seconds = int(str(expires_in))
        if seconds > 0:
            expires_at = datetime.now(UTC) + timedelta(seconds=seconds)
            config["expires_in"] = seconds
            config["issued_at"] = datetime.now(UTC).isoformat()
    return token, expires_at, config


def parse_vk_accounts(text: str) -> tuple[list[VkAccountInput], list[str]]:
    result: list[VkAccountInput] = []
    errors: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        payload = value
        # Legacy `label;token` rows are accepted for a painless upgrade, but the
        # label is deliberately ignored. VK users.get is the identity source.
        if ";" in value and not value.startswith("{"):
            _legacy_label, rest = value.split(";", 1)
            if "access_token=" in rest or len(rest.strip()) >= 20:
                payload = rest.strip()
        try:
            token, expires_at, config = _vk_payload(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"Строка {idx}: {exc}")
            continue
        if len(token) < 20:
            errors.append(f"Строка {idx}: токен выглядит слишком коротким")
            continue
        result.append(
            VkAccountInput(
                line_number=idx,
                token=token,
                expires_at=expires_at,
                config=config,
            )
        )
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
