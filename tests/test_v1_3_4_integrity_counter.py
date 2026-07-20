from __future__ import annotations

from pathlib import Path

from app.db.models import IntegrityCheck
from app.services.integrity import _next_consecutive_gap


def test_new_integrity_row_counter_is_safe_before_flush() -> None:
    # SQLAlchemy insert defaults may not populate Python attributes until flush.
    row = IntegrityCheck(source_id=42)
    assert row.consecutive_gaps is None
    row.consecutive_gaps = _next_consecutive_gap(row.consecutive_gaps)
    assert row.consecutive_gaps == 1


def test_integrity_gap_counter_normalizes_values() -> None:
    assert _next_consecutive_gap(None) == 1
    assert _next_consecutive_gap(0) == 1
    assert _next_consecutive_gap(2) == 3
    assert _next_consecutive_gap(-5) == 1
    assert _next_consecutive_gap("broken") == 1


def test_integrity_service_initializes_counter_explicitly() -> None:
    service = Path("app/services/integrity.py").read_text(encoding="utf-8")
    assert "IntegrityCheck(source_id=source.id, consecutive_gaps=0)" in service
    assert "row.consecutive_gaps = _next_consecutive_gap(row.consecutive_gaps)" in service
    assert "row.consecutive_gaps += 1" not in service


def test_integrity_counter_migration_is_safe() -> None:
    migration = Path("alembic/versions/0006_integrity_counter_guard.py").read_text(encoding="utf-8")
    assert len("0006_integrity_counter_guard") <= 32
    assert 'revision = "0006_integrity_counter_guard"' in migration
    assert 'down_revision = "0005_vk_account_identity"' in migration
    assert "consecutive_gaps = 0 WHERE consecutive_gaps IS NULL" in migration
    assert "nullable=False" in migration
    assert 'server_default=sa.text("0")' in migration
