"""The /config commands. This slice has /config get only.

See ``docs/design.md`` section 3.18 (Settings table). Every /config
command is owner-only and fixed (section 1.2).
"""

import sqlite3

from maika.commands import CommandCategory, CommandDefinition
from maika.database import GLOBAL_GUILD_ID
from maika.foundation_settings import build_foundation_registry
from maika.permissions import Level, PermissionKey
from maika.results import CommandResult, FailureKind
from maika.settings import (
    SettingDefinition,
    UnknownSettingError,
    ValueType,
    describe_range,
    format_value,
)
from maika.settings_store import lookup_value

CONFIG_GET_NAME = "config get"

CONFIG_GET_KEY = PermissionKey("config.get", Level.ADMIN, owner_only=True)

CONFIG_GET_COMMAND = CommandDefinition(
    name=CONFIG_GET_NAME,
    description="Show a setting's current value",
    category=CommandCategory.OWNER,
    key=CONFIG_GET_KEY,
    help_text=(
        "Shows a setting's current value, whether it is the default or "
        "was changed, its description, type, range and default. "
        "Example: /config get key:backups.days_kept"
    ),
    example="/config get key:backups.days_kept",
)

# Every setting defined so far. Later features add their settings.
SETTINGS = build_foundation_registry()

UNKNOWN_SETTING_MESSAGE = "I don't know a setting called `{key}`."
VALUE_LINE = "`{key} = {value}` ({source})"
TYPE_PART = "Type: {value_type}."
RANGE_PART = "Range: {range}."
CHOICES_PART = "Choices: {choices}."
DEFAULT_PART = "Default: {default}."
PART_SEPARATOR = " "
LINE_SEPARATOR = "\n"
SOURCE_DEFAULT = "default"
SOURCE_CHANGED = "changed"

TRUE_TEXT = "true"
FALSE_TEXT = "false"
NONE_TEXT = "none"
EMPTY_LIST_TEXT = "empty list"
LIST_SEPARATOR = ", "

# A backtick would end the reply's code formatting early, so it is
# shown as an apostrophe.
BACKTICK = "`"
BACKTICK_REPLACEMENT = "'"


def _value_text(definition: SettingDefinition, value: object) -> str:
    """Return a value as text, before backticks are replaced."""
    if value is True:
        return TRUE_TEXT
    if value is False:
        return FALSE_TEXT
    if value is None:
        return NONE_TEXT
    if definition.value_type is ValueType.TIME:
        return format_value(definition, value)
    if isinstance(value, (list, tuple)):
        if not value:
            return EMPTY_LIST_TEXT
        return LIST_SEPARATOR.join(str(item) for item in value)
    return str(value)


def display_value(definition: SettingDefinition, value: object) -> str:
    """Return a setting value as text for a reply.

    A bool is ``true`` or ``false``, None is ``none``, a time is
    written as stored (``04:00``), a list is its items joined by ", "
    (or ``empty list``), and anything else uses str(). A backtick is
    shown as an apostrophe.
    """
    text = _value_text(definition, value)
    return text.replace(BACKTICK, BACKTICK_REPLACEMENT)


def _details_line(definition: SettingDefinition) -> str:
    """Return the "Type: ... Range: ... Default: ..." line.

    The middle part is the numeric range, or for a text setting with
    fixed choices the choices. A setting with neither leaves it out.
    """
    parts = [TYPE_PART.format(value_type=definition.value_type.value)]
    setting_range = describe_range(definition)
    if setting_range is not None:
        parts.append(RANGE_PART.format(range=setting_range))
    elif definition.choices is not None:
        choices = LIST_SEPARATOR.join(definition.choices)
        parts.append(CHOICES_PART.format(choices=choices))
    default = display_value(definition, definition.default)
    parts.append(DEFAULT_PART.format(default=default))
    return PART_SEPARATOR.join(parts)


def config_get(
    connection: sqlite3.Connection,
    key: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> CommandResult:
    """Return a setting's current value for ``guild_id``.

    The lookup is the usual order (server row, global row, default).
    The reply has three lines (design section 10, introduction):

        `<key> = <value>` (default or changed)
        <description>
        Type: <type>. Range: <range>. Default: <default>.

    ``(changed)`` means a stored row applies. A text setting with
    fixed choices shows "Choices: ..." in place of "Range: ...", and
    a setting with neither leaves that part out. An unknown key is a
    NOT_FOUND failure.
    """
    try:
        definition = SETTINGS.get(key)
        lookup = lookup_value(connection, SETTINGS, key, guild_id)
    except UnknownSettingError:
        return CommandResult.fail(
            FailureKind.NOT_FOUND, UNKNOWN_SETTING_MESSAGE.format(key=key)
        )
    source = SOURCE_CHANGED if lookup.stored else SOURCE_DEFAULT
    value_line = VALUE_LINE.format(
        key=key,
        value=display_value(definition, lookup.value),
        source=source,
    )
    lines = (value_line, definition.description, _details_line(definition))
    return CommandResult.ok(LINE_SEPARATOR.join(lines))
