"""Write one console log line per command call.

See ``docs/design.md`` section 3.18: command usage goes to
``#bot-log``, and usage of protected keys (``/memories``,
``/forget``) is written at DEBUG level so repeated use cannot flood
the channel. Posting to Discord log channels comes later; for now
the line goes to the ``maika`` logger.

A line names the command and the IDs involved, never the command's
argument values.
"""

import logging

from maika.command_runner import CommandUsage
from maika.log_names import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)

NO_FAILURE_TEXT = "none"


def format_usage_line(usage: CommandUsage) -> str:
    """Return the log line for one command call.

    The decision detail (for example "level 1 (member), feed.subscribe
    needs level 1") follows the decision source in parentheses.
    """
    failure = NO_FAILURE_TEXT
    if usage.failure_kind is not None:
        failure = usage.failure_kind.value
    decided_by = usage.decision_source.value
    if usage.decision_detail is not None:
        decided_by = f"{decided_by} ({usage.decision_detail})"
    return (
        f"Command {usage.command_name} ({usage.key}): "
        f"user {usage.user_id}, guild {usage.guild_id}, "
        f"channel {usage.channel_id}, allowed {usage.allowed}, "
        f"decided by {decided_by}, "
        f"success {usage.success}, failure {failure}"
    )


def log_usage(usage: CommandUsage) -> None:
    """Log one command call: INFO, or DEBUG for a protected key."""
    level = logging.DEBUG if usage.protected else logging.INFO
    logger.log(level, "%s", format_usage_line(usage))
