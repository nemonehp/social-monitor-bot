from __future__ import annotations

import json

from app.db.enums import CredentialPlatform
from app.db.repositories import CredentialRepository
from app.db.session import SessionFactory
from app.security import SecretBox
from app.services.account_importer import TgAccountInput, VkAccountInput


class CredentialManager:
    def __init__(self, key: str):
        self.secret_box = SecretBox(key)

    async def save_vk(self, accounts: list[VkAccountInput]) -> tuple[int, int]:
        created = updated = 0
        async with SessionFactory() as session:
            async with session.begin():
                for account in accounts:
                    _, is_created = await CredentialRepository.add(
                        session,
                        CredentialPlatform.VK,
                        account.label,
                        self.secret_box.encrypt(account.token),
                        account.config,
                        expires_at=account.expires_at,
                    )
                    created += int(is_created)
                    updated += int(not is_created)
        return created, updated

    async def save_tg(self, accounts: list[TgAccountInput]) -> tuple[int, int]:
        created = updated = 0
        async with SessionFactory() as session:
            async with session.begin():
                for account in accounts:
                    _, is_created = await CredentialRepository.add(
                        session,
                        CredentialPlatform.TELEGRAM,
                        account.label,
                        self.secret_box.encrypt(
                            json.dumps({"session": account.session, "api_hash": account.api_hash})
                        ),
                        {
                            "api_id": account.api_id,
                            "device_model": account.device_model,
                            "system_version": account.system_version,
                            "app_version": account.app_version,
                            "system_lang_code": account.system_lang_code,
                            "lang_code": account.lang_code,
                        },
                    )
                    created += int(is_created)
                    updated += int(not is_created)
        return created, updated
