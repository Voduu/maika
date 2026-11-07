import logging
import sqlite3
from pathlib import Path
from unittest import mock

import discord
import pytest

from maika import __main__ as entry
from maika.bot import MaikaClient
from maika.commands import CommandRegistry
from maika.config import Config, ConfigError, Environment

TOKEN = "secret-token-value-should-never-appear"
PROBLEMS = [
    "DISCORD_TOKEN is required but is empty.",
    "OWNER_ID must be a positive integer up to 9223372036854775807.",
]
REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
FOUNDATION_TABLES = {
    "users",
    "permission_overrides",
    "blocks",
    "audit_log",
    "settings",
    "cooldowns",
    "bot_state",
}


def make_config(database_path):
    return Config(
        environment=Environment.TEST,
        discord_token=TOKEN,
        owner_id=1,
        listen_channels=(),
        log_channel_id=None,
        database_path=database_path,
    )


@pytest.fixture(autouse=True)
def real_migrations(monkeypatch):
    # Use an absolute path so the tests do not depend on the folder
    # pytest is started from.
    monkeypatch.setattr(entry, "MIGRATIONS_DIR", REAL_MIGRATIONS_DIR)


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "data" / "maika.db"


@pytest.fixture
def valid_config(monkeypatch, database_path):
    config = make_config(database_path)
    monkeypatch.setattr(entry, "load_config_from_env", lambda: config)
    return config


@pytest.fixture
def config_error(monkeypatch):
    def raise_config_error():
        raise ConfigError(PROBLEMS)

    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setattr(entry, "load_config_from_env", raise_config_error)


@pytest.fixture
def client_class(monkeypatch):
    fake = mock.Mock()
    monkeypatch.setattr(entry, "MaikaClient", fake)
    return fake


# Configuration errors


def test_config_error_returns_1(config_error, client_class):
    assert entry.main() == 1


def test_config_error_prints_each_problem_to_stderr(
    config_error, client_class, capsys
):
    entry.main()
    captured = capsys.readouterr()
    for problem in PROBLEMS:
        assert problem in captured.err
    assert captured.out == ""


def test_config_error_does_not_print_token(config_error, client_class, capsys):
    entry.main()
    captured = capsys.readouterr()
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_config_error_does_not_connect(config_error, client_class):
    entry.main()
    client_class.assert_not_called()


# Database startup


def test_logger_has_fixed_name():
    assert entry.logger.name == "maika"


@pytest.mark.parametrize(
    ("error", "expected_err"),
    [
        (
            PermissionError(13, "Permission denied", "/srv/data/maika.db"),
            "Maika cannot start. [Errno 13] Permission denied: "
            "'/srv/data/maika.db'\n",
        ),
        (
            FileNotFoundError(2, "No such file or directory", "migrations"),
            "Maika cannot start. [Errno 2] No such file or directory: "
            "'migrations'\n",
        ),
        (
            OSError("no file name here"),
            "Maika cannot start. no file name here\n",
        ),
        (
            sqlite3.OperationalError("database is locked"),
            "Maika cannot start. database is locked\n",
        ),
    ],
    ids=[
        "permission-error",
        "file-not-found",
        "oserror-no-filename",
        "sqlite-operational",
    ],
)
def test_startup_os_and_sqlite_errors(
    monkeypatch, valid_config, client_class, capsys, error, expected_err
):
    def raise_error(database_path, now):
        raise error

    monkeypatch.setattr(entry, "prepare_database", raise_error)

    assert entry.main() == 1

    captured = capsys.readouterr()
    assert captured.err == expected_err
    assert TOKEN not in captured.err
    client_class.assert_not_called()


def test_main_creates_database_and_tables(
    valid_config, client_class, database_path, caplog
):
    caplog.set_level(logging.INFO)

    assert entry.main() == 0

    assert database_path.exists()
    connection = sqlite3.connect(database_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    versions = connection.execute(
        "SELECT version FROM schema_migrations"
    ).fetchall()
    connection.close()
    assert FOUNDATION_TABLES <= tables
    assert versions == [(1,)]
    applied_records = [
        record
        for record in caplog.records
        if record.getMessage() == "Applied migration 0001 (foundation)."
    ]
    assert len(applied_records) == 1
    assert applied_records[0].name == "maika"
    client_class.assert_called_once()
    args, kwargs = client_class.call_args
    assert args == (valid_config,)
    assert isinstance(kwargs["connection"], sqlite3.Connection)
    assert isinstance(kwargs["command_registry"], CommandRegistry)
    kwargs["command_registry"].get("config get")


def test_migration_error_returns_1_without_client(
    tmp_path, monkeypatch, valid_config, client_class, capsys
):
    bad_dir = tmp_path / "bad_migrations"
    bad_dir.mkdir()
    (bad_dir / "0001_broken.sql").write_text(
        "CREATE TABLE a (x INTEGER);\nINSERT INTO missing VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(entry, "MIGRATIONS_DIR", bad_dir)

    assert entry.main() == 1

    captured = capsys.readouterr()
    assert "Migration 0001" in captured.err
    assert "missing" in captured.err
    assert TOKEN not in captured.err
    client_class.assert_not_called()


@pytest.fixture
def opened(monkeypatch):
    connections = []
    real_open = entry.open_database

    def recording_open(path):
        connection = real_open(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(entry, "open_database", recording_open)
    return connections


def assert_closed(connection):
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_database_open_while_client_runs_then_closed(
    opened, valid_config, client_class
):
    def check_open(*args, **kwargs):
        assert opened[0].execute("SELECT 1").fetchone()[0] == 1

    client_class.return_value.run.side_effect = check_open

    assert entry.main() == 0
    assert len(opened) == 1
    client_class.return_value.run.assert_called_once()
    assert client_class.call_args.kwargs["connection"] is opened[0]
    assert_closed(opened[0])


def test_database_closed_when_client_raises(
    opened, valid_config, client_class
):
    client_class.return_value.run.side_effect = RuntimeError("crash")

    with pytest.raises(RuntimeError):
        entry.main()
    assert_closed(opened[0])


def test_database_closed_when_migration_fails(
    tmp_path, monkeypatch, opened, valid_config, client_class
):
    bad_dir = tmp_path / "bad_migrations"
    bad_dir.mkdir()
    (bad_dir / "0001_broken.sql").write_text(
        "INSERT INTO missing VALUES (1);\n", encoding="utf-8"
    )
    monkeypatch.setattr(entry, "MIGRATIONS_DIR", bad_dir)

    assert entry.main() == 1
    assert_closed(opened[0])


# Login failure


@pytest.fixture
def login_failure(monkeypatch, valid_config):
    def raise_login_failure(self, token, **kwargs):
        # The token is in the message so the test shows it is not
        # passed on to the console.
        raise discord.LoginFailure(f"Improper token: {token}")

    monkeypatch.setattr(MaikaClient, "run", raise_login_failure)


def test_login_failure_returns_1(login_failure):
    assert entry.main() == 1


def test_login_failure_closes_database(login_failure, opened):
    entry.main()
    assert_closed(opened[0])


def test_login_failure_prints_message_without_token(
    login_failure, capsys, caplog
):
    caplog.set_level(logging.DEBUG)
    entry.main()
    captured = capsys.readouterr()
    assert captured.err == (
        "Discord rejected the token. Check DISCORD_TOKEN in .env.\n"
    )
    assert captured.out == ""
    assert TOKEN not in caplog.text
