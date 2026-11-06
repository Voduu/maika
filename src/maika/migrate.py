"""Find and apply the numbered migration files.

See ``docs/design.md`` section 9.9 for the rules and section 5,
"Database backups", for the backup taken before migrating.

Migration files are named ``NNNN_name.sql`` with four digits, and the
versions run 1, 2, 3 with no gaps. Each file is applied in one
transaction together with its ``schema_migrations`` row, so a file is
either applied completely and recorded, or not at all. A file must
not contain its own ``BEGIN``, ``COMMIT`` or ``ROLLBACK``.

Nothing in this module reads the clock. The caller passes ``now`` in
UTC Unix seconds.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from maika.database import backup_database

MIGRATION_FILE_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>.+)\.sql$")
MIGRATION_FILE_SUFFIX = ".sql"

FIRST_VERSION = 1

# Pre-migration backups kept in the backup folder (design section 5).
MIGRATION_BACKUPS_KEPT = 10

BACKUP_TIME_FORMAT = "%Y%m%d-%H%M%S"

# Only files matching this pattern are ever deleted when old backups
# are pruned. The fixed-width time comes first, so sorting the names
# sorts the backups from oldest to newest.
BACKUP_FILE_PATTERN = re.compile(r"^maika-\d{8}-\d{6}-before-\d{4}\.db$")

CREATE_SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
    "    version     INTEGER PRIMARY KEY,\n"
    "    name        TEXT NOT NULL,\n"
    "    applied_at  INTEGER NOT NULL\n"
    ")"
)

SELECT_APPLIED_VERSIONS_SQL = "SELECT version FROM schema_migrations"

INSERT_MIGRATION_SQL = (
    "INSERT INTO schema_migrations (version, name, applied_at) "
    "VALUES (?, ?, ?)"
)


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    path: Path


class MigrationError(Exception):
    """Raised when the migration files or a migration run fail."""


def find_migrations(directory: Path | str) -> list[Migration]:
    """Return the migration files in ``directory``, sorted by version.

    Files that do not end in ``.sql`` (such as ``.gitkeep``) are
    ignored. Raise MigrationError when a ``.sql`` file does not match
    ``NNNN_name.sql``, so a typo in a name cannot make a migration
    silently skip. Also raise it when two files share a version or
    when the versions do not run 1, 2, 3 with no gaps.
    """
    migrations: list[Migration] = []
    for path in Path(directory).iterdir():
        if not path.is_file() or not path.name.endswith(MIGRATION_FILE_SUFFIX):
            continue
        match = MIGRATION_FILE_PATTERN.match(path.name)
        if match is None:
            raise MigrationError(
                f"Migration file {path.name} does not match the name "
                "pattern NNNN_name.sql (four digits, an underscore, "
                "then a name)."
            )
        migrations.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                path=path,
            )
        )
    migrations.sort(key=lambda migration: (migration.version, migration.name))

    seen: dict[int, Migration] = {}
    for migration in migrations:
        if migration.version in seen:
            raise MigrationError(
                f"Migration version {migration.version:04d} is used by "
                f"two files: {seen[migration.version].path.name} and "
                f"{migration.path.name}."
            )
        seen[migration.version] = migration

    for expected, migration in enumerate(migrations, start=FIRST_VERSION):
        if migration.version != expected:
            raise MigrationError(
                f"Migration versions must run {FIRST_VERSION}, "
                f"{FIRST_VERSION + 1}, {FIRST_VERSION + 2} with no "
                f"gaps. Expected {expected:04d} but found "
                f"{migration.path.name}."
            )

    return migrations


def backup_file_name(now: int, version: int) -> str:
    """Return the pre-migration backup name for a time and version."""
    timestamp = datetime.fromtimestamp(now, tz=UTC)
    return (
        f"maika-{timestamp.strftime(BACKUP_TIME_FORMAT)}"
        f"-before-{version:04d}.db"
    )


def prune_migration_backups(backup_dir: Path) -> None:
    """Delete the oldest pre-migration backups beyond the limit.

    Only files whose names match ``BACKUP_FILE_PATTERN`` are counted
    or deleted. The newest ``MIGRATION_BACKUPS_KEPT`` remain.
    """
    backups = sorted(
        path
        for path in backup_dir.iterdir()
        if path.is_file() and BACKUP_FILE_PATTERN.match(path.name)
    )
    excess = len(backups) - MIGRATION_BACKUPS_KEPT
    for path in backups[: max(excess, 0)]:
        path.unlink()


def _applied_versions(connection: sqlite3.Connection) -> set[int]:
    """Return the versions recorded in ``schema_migrations``."""
    rows = connection.execute(SELECT_APPLIED_VERSIONS_SQL).fetchall()
    return {row[0] for row in rows}


def _apply_one(
    connection: sqlite3.Connection, migration: Migration, now: int
) -> None:
    """Apply one file and record it, in a single transaction.

    ``executescript`` commits any open transaction before it starts,
    so ``BEGIN`` is placed at the start of the script itself. The
    transaction is still open when the script ends, and the record is
    inserted before ``COMMIT``. On any error the whole transaction is
    rolled back.
    """
    sql = migration.path.read_text(encoding="utf-8")
    try:
        connection.executescript("BEGIN;\n" + sql)
        if not connection.in_transaction:
            raise MigrationError(
                f"Migration {migration.version:04d} "
                f"({migration.path.name}) ended its own transaction. "
                "Migration files must not contain BEGIN, COMMIT or "
                "ROLLBACK."
            )
        connection.execute(
            INSERT_MIGRATION_SQL, (migration.version, migration.name, now)
        )
        connection.execute("COMMIT")
    except MigrationError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise MigrationError(
            f"Migration {migration.version:04d} ({migration.path.name}) "
            f"failed and was rolled back: {error}"
        ) from error


def apply_migrations(
    connection: sqlite3.Connection,
    directory: Path | str,
    backup_dir: Path | str | None,
    now: int,
) -> list[Migration]:
    """Apply every pending migration and return the ones applied.

    Steps:

    1. Create ``schema_migrations`` if it is missing.
    2. Raise MigrationError if an applied version has no file.
    3. Return an empty list if nothing is pending.
    4. If ``backup_dir`` is set and at least one migration was already
       applied, back up the database to
       ``maika-YYYYMMDD-HHMMSS-before-NNNN.db`` (UTC time from
       ``now``, NNNN the first pending version), then prune old
       backups. A BackupError stops everything before any migration
       runs.
    5. Apply each pending file in order, each in its own transaction
       with its record. A failing file is rolled back completely and
       raises MigrationError. Files applied before it stay applied.
    """
    migrations = find_migrations(directory)

    connection.execute(CREATE_SCHEMA_MIGRATIONS_SQL)
    applied = _applied_versions(connection)

    known_versions = {migration.version for migration in migrations}
    missing = sorted(applied - known_versions)
    if missing:
        listed = ", ".join(f"{version:04d}" for version in missing)
        raise MigrationError(
            f"The database has applied migration versions with no "
            f"file in {directory}: {listed}."
        )

    pending = [
        migration
        for migration in migrations
        if migration.version not in applied
    ]
    if not pending:
        return []

    if backup_dir is not None and applied:
        backup_dir = Path(backup_dir)
        destination = backup_dir / backup_file_name(now, pending[0].version)
        backup_database(connection, destination)
        prune_migration_backups(backup_dir)

    for migration in pending:
        _apply_one(connection, migration, now)
    return pending
