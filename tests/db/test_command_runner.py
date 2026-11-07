import logging
import sqlite3
from unittest import mock

import pytest

from maika.command_runner import (
    DENIED_MESSAGE,
    FAILURE_MESSAGE,
    CommandContext,
    CommandUsage,
    run_command,
)
from maika.commands import (
    CommandCategory,
    CommandDefinition,
    CommandRegistry,
    UnknownCommandError,
)
from maika.permission_store import set_block, set_level
from maika.permissions import DecisionSource, Level, PermissionKey
from maika.results import CommandResult, FailureKind

NOW = 1_800_000_000
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

CAT_VIEW = PermissionKey("cat.view", Level.EVERYONE)
CAT_RETAG = PermissionKey("cat.retag", Level.TRUSTED)
MEMORY_VIEW = PermissionKey("memory.view", Level.EVERYONE, protected=True)
CHAT_TALK = PermissionKey("chat.talk", Level.EVERYONE)


def define(name, key, category=CommandCategory.CATS):
    return CommandDefinition(
        name=name,
        description=f"The {name} command.",
        category=category,
        key=key,
        help_text=f"Runs {name}.",
        example=f"/{name}",
    )


REGISTRY = CommandRegistry(
    [
        define("cat", CAT_VIEW),
        define("retag", CAT_RETAG, CommandCategory.MANAGE),
        define("memories", MEMORY_VIEW, CommandCategory.ABOUT_ME),
    ],
    extra_keys=[CHAT_TALK],
)

OK_RESULT = CommandResult.ok("Done.", data={"photo_id": 214})


class Recorder:
    """A command function that counts its calls."""

    def __init__(self, result=OK_RESULT, error=None):
        self.calls = 0
        self.result = result
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class UsageLog:
    def __init__(self):
        self.records = []

    def __call__(self, usage):
        self.records.append(usage)


def context(user_id=ALEX, guild_id=GUILD):
    return CommandContext(
        user_id=user_id, guild_id=guild_id, channel_id=CHANNEL, now=NOW
    )


def run(connection, name, function, log=None, user_id=ALEX, guild_id=GUILD):
    if log is None:
        log = UsageLog()
    return run_command(
        connection,
        REGISTRY,
        owner_id=OWNER,
        command_name=name,
        context=context(user_id, guild_id),
        function=function,
        log_usage=log,
    )


def give_level(connection, level, user_id=ALEX):
    set_level(
        connection,
        user_id=user_id,
        level=level,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )


def block_all(connection, user_id=ALEX):
    set_block(
        connection,
        user_id=user_id,
        kind="all",
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )


def test_owner_runs_command(migrated_db):
    function = Recorder()
    log = UsageLog()

    result = run(migrated_db, "retag", function, log, user_id=OWNER)

    assert result is OK_RESULT
    assert function.calls == 1
    assert log.records[0].decision_source is DecisionSource.OWNER


def test_allowed_person_gets_result(migrated_db):
    give_level(migrated_db, Level.TRUSTED)
    function = Recorder()

    result = run(migrated_db, "retag", function)

    assert result is OK_RESULT
    assert function.calls == 1


def test_command_failure_result_is_returned(migrated_db):
    failure = CommandResult.fail(FailureKind.NOT_FOUND, "No such cat.")
    result = run(migrated_db, "cat", Recorder(result=failure))
    assert result is failure


def test_denied_person_never_reaches_function(migrated_db):
    function = Recorder()

    result = run(migrated_db, "retag", function)

    assert result == CommandResult.fail(FailureKind.DENIED, DENIED_MESSAGE)
    assert function.calls == 0


def test_all_block_denies_but_protected_runs(migrated_db):
    give_level(migrated_db, Level.ADMIN)
    block_all(migrated_db)
    cat = Recorder()
    memories = Recorder()

    denied = run(migrated_db, "cat", cat)
    allowed = run(migrated_db, "memories", memories)

    assert denied.failure_kind is FailureKind.DENIED
    assert cat.calls == 0
    assert allowed is OK_RESULT
    assert memories.calls == 1


def test_direct_message_uses_global_rows(migrated_db):
    give_level(migrated_db, Level.TRUSTED)
    result = run(migrated_db, "retag", Recorder(), guild_id=0)
    assert result is OK_RESULT


def test_raising_command_returns_other_failure(migrated_db, caplog):
    failing = Recorder(error=RuntimeError("secret value 42"))

    with caplog.at_level(logging.ERROR, logger="maika.command_runner"):
        result = run(migrated_db, "cat", failing)

    assert result == CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)
    assert failing.calls == 1
    assert caplog.records[0].getMessage() == "Command cat raised an error."
    assert caplog.records[0].exc_info is not None

    assert run(migrated_db, "cat", Recorder()) is OK_RESULT


@pytest.mark.parametrize("returned", [None, "Done.", {"success": True}])
def test_non_result_return_is_failure(migrated_db, caplog, returned):
    with caplog.at_level(logging.ERROR, logger="maika.command_runner"):
        result = run(migrated_db, "cat", Recorder(result=returned))

    assert result == CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)
    message = caplog.records[0].getMessage()
    assert message.startswith("Command cat returned ")
    assert "Done." not in message


def test_usage_for_allowed_call(migrated_db):
    give_level(migrated_db, Level.TRUSTED)
    log = UsageLog()

    run(migrated_db, "retag", Recorder(), log)

    assert log.records == [
        CommandUsage(
            command_name="retag",
            key="cat.retag",
            protected=False,
            user_id=ALEX,
            guild_id=GUILD,
            channel_id=CHANNEL,
            now=NOW,
            allowed=True,
            decision_source=DecisionSource.LEVEL,
            decision_detail="level 2 (trusted), cat.retag needs level 2",
            success=True,
            failure_kind=None,
        )
    ]


def test_usage_for_denied_call(migrated_db):
    log = UsageLog()

    run(migrated_db, "retag", Recorder(), log)

    (usage,) = log.records
    assert usage.allowed is False
    assert usage.decision_source is DecisionSource.LEVEL
    assert usage.decision_detail == (
        "level 0 (everyone), cat.retag needs level 2"
    )
    assert usage.success is False
    assert usage.failure_kind is FailureKind.DENIED


def test_usage_for_blocked_call(migrated_db):
    block_all(migrated_db)
    log = UsageLog()

    run(migrated_db, "cat", Recorder(), log)

    assert log.records[0].decision_source is DecisionSource.BLOCK
    assert log.records[0].allowed is False


def test_usage_for_failed_call(migrated_db):
    log = UsageLog()

    run(migrated_db, "cat", Recorder(error=ValueError("x")), log)

    (usage,) = log.records
    assert usage.allowed is True
    assert usage.success is False
    assert usage.failure_kind is FailureKind.OTHER


def test_usage_protected_flag(migrated_db):
    log = UsageLog()

    run(migrated_db, "memories", Recorder(), log)
    run(migrated_db, "cat", Recorder(), log)

    assert [(u.command_name, u.protected) for u in log.records] == [
        ("memories", True),
        ("cat", False),
    ]
    assert log.records[0].decision_source is DecisionSource.PROTECTED


def test_failing_log_usage_keeps_result(migrated_db, caplog):
    def broken_log(usage):
        raise RuntimeError("log down")

    function = Recorder()
    with caplog.at_level(logging.ERROR, logger="maika.command_runner"):
        result = run(migrated_db, "cat", function, broken_log)

    assert result is OK_RESULT
    assert function.calls == 1
    assert caplog.records[0].getMessage() == (
        "Logging the usage of cat failed."
    )


def test_failing_log_usage_keeps_denied_result(migrated_db):
    def broken_log(usage):
        raise RuntimeError("log down")

    result = run(migrated_db, "retag", Recorder(), broken_log)
    assert result.failure_kind is FailureKind.DENIED


def test_unknown_command_raises(migrated_db):
    function = Recorder()
    log = UsageLog()

    with pytest.raises(UnknownCommandError):
        run(migrated_db, "dog", function, log)

    assert function.calls == 0
    assert log.records == []


def test_runner_writes_nothing(migrated_db):
    before = migrated_db.total_changes

    run(migrated_db, "cat", Recorder())
    run(migrated_db, "retag", Recorder())

    assert migrated_db.total_changes == before
    assert not migrated_db.in_transaction


def test_permission_check_error_returns_other_failure(migrated_db, caplog):
    function = Recorder()
    log = UsageLog()
    error = sqlite3.OperationalError("database is locked")

    with mock.patch(
        "maika.command_runner.check_permission", side_effect=error
    ):
        with caplog.at_level(logging.ERROR, logger="maika.command_runner"):
            result = run(migrated_db, "cat", function, log)

    assert result == CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)
    assert function.calls == 0
    assert log.records == []
    assert caplog.records[0].getMessage() == (
        "The permission check for cat failed."
    )
    assert caplog.records[0].exc_info is not None
