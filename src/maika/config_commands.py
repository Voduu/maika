"""The /config commands: get, set, reset and list.

See ``docs/design.md`` section 3.18 (Settings table, Change log).
Every /config command is owner-only and fixed (section 1.2). Set and
reset write global rows only for now, each with an audit entry, and
announce the change through a callable (the bot posts it to
``#bot-log``).
"""

import logging
import sqlite3
from collections.abc import Callable

from maika.commands import CommandCategory, CommandDefinition
from maika.database import GLOBAL_GUILD_ID
from maika.foundation_settings import build_foundation_registry
from maika.permissions import Level, PermissionKey
from maika.results import CommandResult, FailureKind
from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    SettingValueError,
    UnknownSettingError,
    ValueType,
    describe_range,
    format_value,
)
from maika.settings_store import (
    lookup_value,
    reset_setting_value,
    set_setting_value,
)

logger = logging.getLogger(__name__)

type AnnounceFunction = Callable[[str], None]

CONFIG_GET_NAME = "config get"
CONFIG_SET_NAME = "config set"
CONFIG_RESET_NAME = "config reset"
CONFIG_LIST_NAME = "config list"

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

CONFIG_SET_KEY = PermissionKey("config.set", Level.ADMIN, owner_only=True)

CONFIG_SET_COMMAND = CommandDefinition(
    name=CONFIG_SET_NAME,
    description="Change a setting's value",
    category=CommandCategory.OWNER,
    key=CONFIG_SET_KEY,
    help_text=(
        "Changes a setting everywhere. The value is checked against the "
        "setting's type and range, and the change is logged. "
        "Example: /config set key:backups.days_kept value:14"
    ),
    example="/config set key:backups.days_kept value:14",
)

CONFIG_RESET_KEY = PermissionKey("config.reset", Level.ADMIN, owner_only=True)

CONFIG_RESET_COMMAND = CommandDefinition(
    name=CONFIG_RESET_NAME,
    description="Put a setting back to its default",
    category=CommandCategory.OWNER,
    key=CONFIG_RESET_KEY,
    help_text=(
        "Removes a setting's override so its default applies again, "
        "and logs the change. "
        "Example: /config reset key:backups.days_kept"
    ),
    example="/config reset key:backups.days_kept",
)

CONFIG_LIST_KEY = PermissionKey("config.list", Level.ADMIN, owner_only=True)

CONFIG_LIST_COMMAND = CommandDefinition(
    name=CONFIG_LIST_NAME,
    description="List settings by category",
    category=CommandCategory.OWNER,
    key=CONFIG_LIST_KEY,
    help_text=(
        "Without a category, lists the categories and how many settings "
        "each has. With a category, lists its settings and their values. "
        "Basic settings only, unless all is true. "
        "Example: /config list category:logging all:true"
    ),
    example="/config list category:logging all:true",
)

# Every setting defined so far. Later features add their settings.
SETTINGS = build_foundation_registry()

UNKNOWN_SETTING_MESSAGE = "I don't know a setting called `{key}`."
REJECTED_VALUE_MESSAGE = "`{key}` {rule}."
SET_MESSAGE = "`{key}` is now `{new}` (was `{old}`)."
SET_DEFAULT_NOTE = (
    " This is the default value. `/config reset {key}` removes the "
    "override, so later changes to the default apply."
)
ALREADY_SET_MESSAGE = "`{key}` is already `{value}`."
RESET_MESSAGE = "`{key}` is back to its default, `{default}` (was `{old}`)."
ALREADY_DEFAULT_MESSAGE = "`{key}` already uses its default."
SET_ANNOUNCE_LINE = "Setting changed: `{key}` `{old}` to `{new}`."
RESET_ANNOUNCE_LINE = (
    "Setting reset: `{key}` `{old}` to its default `{default}`."
)
CATEGORY_LINE = "{name} ({count})"
CATEGORY_HINT_LINE = "Use `/config list category:<name>`."
LIST_VALUE_LINE = "`{key} = {value}`"
LIST_CHANGED_SUFFIX = " (changed)"
UNKNOWN_CATEGORY_MESSAGE = (
    "I don't know a settings category called `{name}`. "
    "Categories: {categories}."
)
NO_BASIC_SETTINGS_MESSAGE = (
    "No Basic settings in `{name}`. Add `all:true` to include Advanced "
    "settings."
)
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


def setting_categories(registry: SettingsRegistry) -> tuple[str, ...]:
    """Return the category names, in the order they first appear."""
    categories: list[str] = []
    for key in registry.keys():
        category = registry.get(key).category
        if category not in categories:
            categories.append(category)
    return tuple(categories)


def _listed_definitions(
    registry: SettingsRegistry, category: str, show_all: bool
) -> tuple[SettingDefinition, ...]:
    """Return a category's settings, Basic only unless ``show_all``."""
    definitions = registry.definitions_in_category(category)
    if show_all:
        return definitions
    return tuple(
        definition
        for definition in definitions
        if definition.level is SettingLevel.BASIC
    )


def _category_overview(
    registry: SettingsRegistry, show_all: bool
) -> CommandResult:
    """Return one line per category with its count, then a hint."""
    lines = [
        CATEGORY_LINE.format(
            name=category,
            count=len(_listed_definitions(registry, category, show_all)),
        )
        for category in setting_categories(registry)
    ]
    lines.append(CATEGORY_HINT_LINE)
    return CommandResult.ok(LINE_SEPARATOR.join(lines))


def _category_listing(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    category: str,
    show_all: bool,
) -> CommandResult:
    """Return one "`key = value`" line per listed setting."""
    definitions = _listed_definitions(registry, category, show_all)
    if not definitions:
        return CommandResult.ok(
            NO_BASIC_SETTINGS_MESSAGE.format(name=category)
        )
    lines = []
    for definition in definitions:
        lookup = lookup_value(
            connection, registry, definition.key, GLOBAL_GUILD_ID
        )
        line = LIST_VALUE_LINE.format(
            key=definition.key,
            value=display_value(definition, lookup.value),
        )
        if lookup.stored:
            line += LIST_CHANGED_SUFFIX
        lines.append(line)
    return CommandResult.ok(LINE_SEPARATOR.join(lines))


def config_list(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    category: str | None,
    show_all: bool,
) -> CommandResult:
    """List the setting categories, or the settings in one category.

    With no category, each category is listed with the number of
    settings that would be shown. With a category (matched without
    regard to case), each setting is shown as "`key = value`" with
    the /config get display rules, plus " (changed)" when a stored
    global row applies. Only Basic settings are counted and listed
    unless ``show_all`` is true. An unknown category is NOT_FOUND.
    """
    categories = setting_categories(registry)
    if category is None or category.strip() == "":
        return _category_overview(registry, show_all)

    name = category.strip().lower()
    if name not in categories:
        return CommandResult.fail(
            FailureKind.NOT_FOUND,
            UNKNOWN_CATEGORY_MESSAGE.format(
                name=category.strip(),
                categories=LIST_SEPARATOR.join(categories),
            ),
        )
    return _category_listing(connection, registry, name, show_all)


def _unknown_setting(key: str) -> CommandResult:
    """Return the NOT_FOUND failure for an unknown key."""
    return CommandResult.fail(
        FailureKind.NOT_FOUND, UNKNOWN_SETTING_MESSAGE.format(key=key)
    )


def _rejected_value(error: SettingValueError) -> CommandResult:
    """Return the INVALID failure for a rejected value."""
    message = REJECTED_VALUE_MESSAGE.format(key=error.key, rule=error.rule)
    return CommandResult.fail(FailureKind.INVALID, message)


def _announce(announce: AnnounceFunction, line: str) -> None:
    """Call ``announce``, logging a failure instead of raising.

    The change is already saved, so a failed announcement must not
    turn the reply into a failure.
    """
    try:
        announce(line)
    except Exception:
        logger.exception("Announcing a settings change failed.")


def config_set(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    value: str,
    *,
    actor_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Set a setting everywhere (``guild_id`` 0) and audit it.

    The reply shows the new and old values with the /config get
    display rules, plus a note when the new value is the default. A
    value that stores the same as the current row writes nothing and
    replies "already". An unknown key is NOT_FOUND, and a rejected
    value is INVALID with the rule. When something changed,
    ``announce`` receives the change line.
    """
    try:
        definition = registry.get(key)
        result = set_setting_value(
            connection,
            registry,
            key=key,
            text=value,
            guild_id=GLOBAL_GUILD_ID,
            actor_id=actor_id,
            now=now,
        )
    except UnknownSettingError:
        return _unknown_setting(key)
    except SettingValueError as error:
        return _rejected_value(error)

    new = display_value(definition, result.new_value)
    if not result.changed:
        return CommandResult.ok(
            ALREADY_SET_MESSAGE.format(key=key, value=new), data=result
        )

    old = display_value(definition, result.old_value)
    message = SET_MESSAGE.format(key=key, new=new, old=old)
    if result.is_default:
        message += SET_DEFAULT_NOTE.format(key=key)
    _announce(announce, SET_ANNOUNCE_LINE.format(key=key, old=old, new=new))
    return CommandResult.ok(message, data=result)


def config_reset(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    *,
    actor_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Remove a setting's global override so the default applies.

    The reply shows the default and the old value. With no override
    nothing is written and the reply says the default is already in
    use. An unknown key is NOT_FOUND. When something changed,
    ``announce`` receives the reset line.
    """
    try:
        definition = registry.get(key)
        result = reset_setting_value(
            connection,
            registry,
            key=key,
            guild_id=GLOBAL_GUILD_ID,
            actor_id=actor_id,
            now=now,
        )
    except UnknownSettingError:
        return _unknown_setting(key)
    except SettingValueError as error:
        return _rejected_value(error)

    if not result.changed:
        return CommandResult.ok(
            ALREADY_DEFAULT_MESSAGE.format(key=key), data=result
        )

    old = display_value(definition, result.old_value)
    default = display_value(definition, result.new_value)
    _announce(
        announce,
        RESET_ANNOUNCE_LINE.format(key=key, old=old, default=default),
    )
    return CommandResult.ok(
        RESET_MESSAGE.format(key=key, default=default, old=old), data=result
    )
