# Validation performed before packaging

The following checks were executed against this archive:

- Python syntax compilation for `app`, `alembic`, and `tests`.
- Import of all runtime modules with a valid dummy configuration.
- SQLAlchemy mapper configuration.
- PostgreSQL DDL generation through a mock PostgreSQL engine.
- YAML parsing of `docker-compose.yml`.
- Pyflakes static check with no remaining findings.
- Unit tests for VK/TG link normalization, proxy formats, region-aware CSV import.
- Import smoke test against the supplied XLSX table:
  - 89 data rows;
  - 160 accepted VK/TG sources;
  - 0 parsing errors.

A live end-to-end test against real Telegram/VK credentials and real proxies is intentionally not embedded in the archive because no production secrets were provided.
