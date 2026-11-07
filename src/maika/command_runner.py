"""The wrapper every command runs through.

See ``docs/design.md`` section 1.2: one wrapper around every command
does the permission check and logs usage. ``run_command`` checks the
command's key, runs the command function when allowed, turns a crash
into a plain failure, and passes a usage record to ``log_usage``.

Nothing here reads the clock (``now`` comes from the context) or
writes to the database.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from maika.commands import CommandRegistry
from maika.permissions import DecisionSource, check_permission
from maika.results import CommandResult, FailureKind

logger = logging.getLogger(__name__)

# The default refusal until the denied pool (3.9) is built.
DENIED_MESSAGE = "You don't have permission for that."
FAILURE_MESSAGE = "Something went wrong."


@dataclass(frozen=True)
class CommandContext:
    """Who ran a command, where, and when.

    ``guild_id`` is 0 for a direct message. ``now`` is UTC Unix
    seconds.
    """

    user_id: int
    guild_id: int
    channel_id: int
    now: int


@dataclass(frozen=True)
class CommandUsage:
    """The usage record of one command call, for the log layer.

    ``protected`` comes from the command's key, so the log layer can
    write protected-key usage at a lower level (design 3.18).
    """

    command_name: str
    key: str
    protected: bool
    user_id: int
    guild_id: int
    channel_id: int
    now: int
    allowed: bool
    decision_source: DecisionSource
    decision_detail: str
    success: bool
    failure_kind: FailureKind | None


def _call_function(
    command_name: str, function: Callable[[], CommandResult]
) -> CommandResult:
    """Run a command function and return its result.

    A crash or a return value that is not a CommandResult is logged
    and becomes an OTHER failure. The log names the command only,
    never its arguments or values.
    """
    try:
        result = function()
    except Exception:
        logger.exception("Command %s raised an error.", command_name)
        return CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)
    if not isinstance(result, CommandResult):
        logger.error(
            "Command %s returned %s instead of a CommandResult.",
            command_name,
            type(result).__name__,
        )
        return CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)
    return result


def run_command(
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    *,
    owner_id: int,
    command_name: str,
    context: CommandContext,
    function: Callable[[], CommandResult],
    log_usage: Callable[[CommandUsage], None],
) -> CommandResult:
    """Check permission, run a command, and log its usage.

    A refusal returns a DENIED failure without calling ``function``.
    A crash in the permission check returns an OTHER failure, with no
    call and no usage record, because no decision was made.
    Every call, protected keys included, produces one CommandUsage
    for ``log_usage``; a crash in ``log_usage`` is logged and does
    not change the result.

    Raise UnknownCommandError for an unknown command name, which is
    a programming error.
    """
    definition = registry.get(command_name)
    try:
        decision = check_permission(
            connection,
            registry.permission_registry(),
            owner_id=owner_id,
            user_id=context.user_id,
            key=definition.key.key,
            guild_id=context.guild_id,
        )
    except Exception:
        # No decision was made, so the function is not called and
        # there is no usage record.
        logger.exception("The permission check for %s failed.", command_name)
        return CommandResult.fail(FailureKind.OTHER, FAILURE_MESSAGE)

    if decision.allowed:
        result = _call_function(command_name, function)
    else:
        result = CommandResult.fail(FailureKind.DENIED, DENIED_MESSAGE)

    usage = CommandUsage(
        command_name=command_name,
        key=definition.key.key,
        protected=definition.key.protected,
        user_id=context.user_id,
        guild_id=context.guild_id,
        channel_id=context.channel_id,
        now=context.now,
        allowed=decision.allowed,
        decision_source=decision.source,
        decision_detail=decision.detail,
        success=result.success,
        failure_kind=result.failure_kind,
    )
    try:
        log_usage(usage)
    except Exception:
        logger.exception("Logging the usage of %s failed.", command_name)
    return result
