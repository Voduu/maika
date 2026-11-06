import logging
from pathlib import Path
from unittest import mock

import discord
import pytest

from maika import __main__ as entry
from maika.bot import MaikaClient
from maika.config import Config, ConfigError, Environment

TOKEN = "secret-token-value-should-never-appear"
PROBLEMS = [
    "DISCORD_TOKEN is required but is empty.",
    "OWNER_ID must be a positive integer up to 9223372036854775807.",
]


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


@pytest.fixture
def login_failure(monkeypatch):
    config = Config(
        environment=Environment.TEST,
        discord_token=TOKEN,
        owner_id=1,
        listen_channels=(),
        log_channel_id=None,
        database_path=Path("unused.db"),
    )

    def raise_login_failure(self, token, **kwargs):
        # The token is in the message so the test shows it is not
        # passed on to the console.
        raise discord.LoginFailure(f"Improper token: {token}")

    monkeypatch.setattr(entry, "load_config_from_env", lambda: config)
    monkeypatch.setattr(MaikaClient, "run", raise_login_failure)


def test_login_failure_returns_1(login_failure):
    assert entry.main() == 1


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
