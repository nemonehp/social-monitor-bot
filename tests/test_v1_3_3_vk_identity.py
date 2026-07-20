from __future__ import annotations

from pathlib import Path

import pytest

from app.collectors.errors import CredentialDeadError
from app.collectors.vk import VkCollector
from app.services.account_importer import parse_vk_accounts


def test_vk_import_ignores_legacy_user_label() -> None:
    token = "a" * 40
    accounts, errors = parse_vk_accounts(f"my-manual-label;{token}")

    assert errors == []
    assert len(accounts) == 1
    assert accounts[0].line_number == 1
    assert accounts[0].token == token
    assert not hasattr(accounts[0], "label")


def test_vk_identity_is_derived_from_users_get_response() -> None:
    identity = VkCollector._identity_from_response(
        {
            "response": [
                {
                    "id": 285495652,
                    "first_name": "Елена",
                    "last_name": "Слобода",
                    "screen_name": "elena_example",
                }
            ]
        }
    )

    assert identity.user_id == 285495652
    assert identity.label == "vk-285495652"
    assert identity.display_name == "Елена Слобода"
    assert identity.screen_name == "elena_example"


def test_vk_identity_rejects_empty_response() -> None:
    with pytest.raises(CredentialDeadError):
        VkCollector._identity_from_response({"response": []})


def test_vk_import_uses_users_get_and_not_input_label() -> None:
    collector = Path("app/collectors/vk.py").read_text(encoding="utf-8")
    manager = Path("app/services/credential_manager.py").read_text(encoding="utf-8")
    handlers = Path("app/bot/handlers.py").read_text(encoding="utf-8")

    assert 'token, "users.get", params' in collector
    vk_save = manager.split("async def save_vk", 1)[1].split("async def save_tg", 1)[0]
    assert "upsert_vk_identity" in vk_save
    assert "account.label" not in vk_save
    assert "Label указывать не нужно" in handlers


def test_vk_identity_migration_is_safe() -> None:
    migration = Path("alembic/versions/0005_vk_account_identity.py").read_text(encoding="utf-8")
    models = Path("app/db/models.py").read_text(encoding="utf-8")

    assert len("0005_vk_account_identity") <= 32
    assert 'revision = "0005_vk_account_identity"' in migration
    assert 'down_revision = "0004_capacity_forum_integrity"' in migration
    assert "external_account_id" in migration
    assert "known.copies = 1" in migration
    assert "SET label = 'vk-' || external_account_id" in migration
    assert "uq_credentials_platform_external_account" in migration
    assert "external_account_id" in models


def test_vk_repository_merges_known_duplicates_without_using_legacy_label_as_identity() -> None:
    repository = Path("app/db/repositories.py").read_text(encoding="utf-8")

    assert 'Credential.config_json["user_id"].astext == identity' in repository
    assert "_merge_duplicate_usage" in repository
    assert 'label_owner.label = f"vk-legacy-{label_owner.id}"' in repository
    assert "Credential.label == canonical_label" not in repository.split(
        "async def upsert_vk_identity", 1
    )[1].split("label_owner =", 1)[0]
