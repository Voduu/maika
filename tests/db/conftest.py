from pathlib import Path

import pytest

from maika.database import open_database
from maika.migrate import apply_migrations

REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# 2027-01-15 08:00:00 UTC
MIGRATED_AT = 1_800_000_000


@pytest.fixture
def real_migrations_dir():
    """The repository's migrations/ folder."""
    return REAL_MIGRATIONS_DIR


@pytest.fixture
def migrated_db():
    """A fresh in-memory database with every real migration applied."""
    connection = open_database(":memory:")
    apply_migrations(connection, REAL_MIGRATIONS_DIR, None, MIGRATED_AT)
    yield connection
    connection.close()
