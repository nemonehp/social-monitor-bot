import os
import sys
from pathlib import Path

# Modules that create the async session factory read settings at import time.
# Test values are local placeholders; no network or database connection is made.
os.environ.setdefault("BOT_TOKEN", "123456:test_token")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "123456789")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://social_monitor:test@localhost:5432/social_monitor_test",
)
os.environ.setdefault(
    "APP_ENCRYPTION_KEY",
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
