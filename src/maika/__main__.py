"""Entry point: ``python -m maika``."""

import logging
import sqlite3
import sys
import time
from pathlib import Path

import discord

from maika.bot import MaikaClient
from maika.config import ConfigError, load_config_from_env
from maika.database import BackupError, JournalModeError, open_database
from maika.migrate import MigrationError, apply_migrations

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_LEVEL = logging.INFO

# Relative to the working directory the bot is started from.
MIGRATIONS_DIR = Path("migrations")

# Pre-migration backups go here, relative to the database's folder.
MIGRATION_BACKUP_SUBDIR = Path("backups") / "migrations"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_DATABASE_ERROR = 1
EXIT_LOGIN_FAILURE = 1

# A fixed name, because __name__ is "__main__" when the bot runs as
# ``python -m maika``.
LOGGER_NAME = "maika"

logger = logging.getLogger(LOGGER_NAME)

LOGIN_FAILURE_MESSAGE = (
    "Discord rejected the token. Check DISCORD_TOKEN in .env."
)


def configure_logging() -> None:
    """Send log records to the console in a simple format."""
    logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL)


def prepare_database(database_path: Path, now: int) -> None:
    """Open the database, apply pending migrations, then close it.

    Each applied migration is logged at INFO level. MigrationError,
    BackupError and JournalModeError pass to the caller, and the
    connection is closed either way.
    """
    backup_dir = database_path.parent / MIGRATION_BACKUP_SUBDIR
    connection = open_database(database_path)
    try:
        applied = apply_migrations(connection, MIGRATIONS_DIR, backup_dir, now)
        for migration in applied:
            logger.info(
                "Applied migration %04d (%s).",
                migration.version,
                migration.name,
            )
    finally:
        # Nothing uses the database yet. Later tasks keep this
        # connection open and pass it to the client.
        connection.close()


def main() -> int:
    """Load the config, run the bot and return the exit status.

    A configuration problem is printed to stderr and returns
    ``EXIT_CONFIG_ERROR`` without connecting to Discord. A database
    that cannot be opened, backed up or migrated (including file
    system and SQLite errors) prints the error to stderr and returns
    ``EXIT_DATABASE_ERROR``, also without connecting. A token that
    Discord rejects prints ``LOGIN_FAILURE_MESSAGE`` to stderr and
    returns ``EXIT_LOGIN_FAILURE``.
    """
    configure_logging()

    try:
        config = load_config_from_env()
    except ConfigError as error:
        print("Maika cannot start. Configuration problems:", file=sys.stderr)
        for problem in error.problems:
            print(f"- {problem}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        prepare_database(config.database_path, now=int(time.time()))
    except (
        MigrationError,
        BackupError,
        JournalModeError,
        OSError,
        sqlite3.Error,
    ) as error:
        # str() of an OSError that has a file name already includes
        # it, for example "[Errno 13] Permission denied: 'var'".
        print(f"Maika cannot start. {error}", file=sys.stderr)
        return EXIT_DATABASE_ERROR

    client = MaikaClient(config)
    # log_handler=None stops discord.py from adding its own console
    # handler, so its records use the format set above.
    try:
        client.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        # The exception is not logged or printed, so nothing that
        # could hold the token reaches the console.
        print(LOGIN_FAILURE_MESSAGE, file=sys.stderr)
        return EXIT_LOGIN_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
