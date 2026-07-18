from pathlib import Path


def test_checkpoint_migration_uses_database_enum_label() -> None:
    migration = Path("alembic/versions/0002_checkpoint_monitoring.py").read_text()
    assert "status::text <> 'DELETED'" in migration
    assert "status <> 'deleted'" not in migration
