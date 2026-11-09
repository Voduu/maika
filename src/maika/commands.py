"""Command definitions and the registry that holds them.

See ``docs/design.md`` sections 1.2 (the command registry and
permission keys) and 2.6 (help categories). Each command is defined
once here; the wrapper in ``command_runner`` checks its key, and
``/help`` and ``/perm keys`` will read the same registry.
"""

import enum
import re
from collections.abc import Iterable
from dataclasses import dataclass

from maika.permissions import PermissionKey, PermissionRegistry

# Lowercase words (letters, digits, hyphens, underscores) separated
# by single spaces, for example "feed subscribe".
COMMAND_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+( [a-z0-9_-]+)*$")
COMMAND_NAME_SEPARATOR = " "

# Discord's limits for a command name: a command, a group and a
# subcommand at most, each word 1 to 32 characters.
COMMAND_NAME_MAX_WORDS = 3
COMMAND_WORD_MAX_LENGTH = 32

# Discord's limit for a command description.
DESCRIPTION_MIN_LENGTH = 1
DESCRIPTION_MAX_LENGTH = 100

# Joins the descriptions of several commands that share one key.
KEY_DESCRIPTION_SEPARATOR = "; "


class CommandCategory(enum.Enum):
    """The help categories of design section 2.6.

    The value is the category's display name.
    """

    CATS = "Cats"
    REMINDERS_AND_TIME = "Reminders and time"
    FEEDS = "Feeds"
    ABOUT_ME = "About me"
    FUN = "Fun"
    MANAGE = "Manage"
    OWNER = "Owner"


class Visibility(enum.Enum):
    """Who sees a command's reply (design section 1.5)."""

    PUBLIC = "public"
    EPHEMERAL = "ephemeral"


class UnknownCommandError(Exception):
    """Raised when a command name has no definition in the registry."""

    def __init__(self, name: str) -> None:
        """Store the name and build the message."""
        self.name = name
        super().__init__(f"There is no command named {name!r}.")


@dataclass(frozen=True)
class CommandDefinition:
    """One command and what the wrapper and ``/help`` need to know.

    ``name`` is the full command name, such as ``feed subscribe``.
    ``key`` is the permission key the wrapper checks.
    ``visibility`` defaults to EPHEMERAL, so a reply is private unless
    the definition says otherwise.
    """

    name: str
    description: str
    category: CommandCategory
    key: PermissionKey
    help_text: str
    example: str
    visibility: Visibility = Visibility.EPHEMERAL

    def __post_init__(self) -> None:
        """Raise ValueError when the definition is wrong."""
        if not isinstance(self.name, str) or not COMMAND_NAME_PATTERN.match(
            self.name
        ):
            raise ValueError(
                f"Command name {self.name!r} must be lowercase words "
                "(letters, digits, hyphens and underscores) separated "
                "by single spaces."
            )
        words = self.name.split(COMMAND_NAME_SEPARATOR)
        if len(words) > COMMAND_NAME_MAX_WORDS:
            raise ValueError(
                f"Command name {self.name!r} has {len(words)} words; "
                f"Discord allows at most {COMMAND_NAME_MAX_WORDS}."
            )
        for word in words:
            if len(word) > COMMAND_WORD_MAX_LENGTH:
                raise ValueError(
                    f"Command name {self.name!r}: the word {word!r} has "
                    f"{len(word)} characters; Discord allows at most "
                    f"{COMMAND_WORD_MAX_LENGTH}."
                )
        description_length = len(self.description.strip())
        if (
            description_length < DESCRIPTION_MIN_LENGTH
            or len(self.description) > DESCRIPTION_MAX_LENGTH
        ):
            raise ValueError(
                f"{self.name}: the description must be "
                f"{DESCRIPTION_MIN_LENGTH} to {DESCRIPTION_MAX_LENGTH} "
                f"characters, not {len(self.description)}."
            )
        if not isinstance(self.category, CommandCategory):
            raise ValueError(
                f"{self.name}: category must be a CommandCategory, not "
                f"{self.category!r}."
            )
        if not isinstance(self.key, PermissionKey):
            raise ValueError(
                f"{self.name}: key must be a PermissionKey, not {self.key!r}."
            )
        if self.help_text.strip() == "":
            raise ValueError(f"{self.name}: help_text must not be empty.")
        if self.example.strip() == "":
            raise ValueError(f"{self.name}: example must not be empty.")
        if not isinstance(self.visibility, Visibility):
            raise ValueError(
                f"{self.name}: visibility must be a Visibility, not "
                f"{self.visibility!r}."
            )


class CommandRegistry:
    """Every command definition, plus keys that are not commands."""

    def __init__(
        self,
        commands: Iterable[CommandDefinition],
        extra_keys: Iterable[PermissionKey] = (),
    ) -> None:
        """Index the commands and collect their permission keys.

        ``extra_keys`` holds keys that belong to no command, such as
        ``chat.talk``. Raise ValueError for a duplicate command name,
        or when two different PermissionKey objects share one key.
        The PermissionRegistry is built here, so its own checks (such
        as owner-only areas) also run at startup.
        """
        self._commands: dict[str, CommandDefinition] = {}
        keys: dict[str, PermissionKey] = {}

        for definition in commands:
            if definition.name in self._commands:
                raise ValueError(
                    f"Command {definition.name} is defined more than once."
                )
            self._commands[definition.name] = definition
            _add_key(keys, definition.key)

        for extra in extra_keys:
            _add_key(keys, extra)

        self._permission_registry = PermissionRegistry(keys.values())

    def get(self, name: str) -> CommandDefinition:
        """Return the definition for a command name.

        Raise UnknownCommandError when there is none.
        """
        try:
            return self._commands[name]
        except KeyError:
            raise UnknownCommandError(name) from None

    def commands(self) -> tuple[CommandDefinition, ...]:
        """Return every definition, sorted by name."""
        return tuple(self._commands[name] for name in sorted(self._commands))

    def commands_in_category(
        self, category: CommandCategory
    ) -> tuple[CommandDefinition, ...]:
        """Return the definitions in one category, sorted by name."""
        return tuple(
            definition
            for definition in self.commands()
            if definition.category is category
        )

    def permission_registry(self) -> PermissionRegistry:
        """Return a PermissionRegistry of every distinct key."""
        return self._permission_registry

    def key_description(self, key: str) -> str:
        """Return a one-line description of a permission key.

        That is the key's own description when it has one, otherwise
        the descriptions of the commands that use the key, sorted by
        command name and joined with "; ". A key with neither returns
        "". Raise UnknownPermissionKeyError for an unknown key.
        """
        entry = self._permission_registry.get(key)
        if entry.description:
            return entry.description
        return KEY_DESCRIPTION_SEPARATOR.join(
            definition.description
            for definition in self.commands()
            if definition.key.key == key
        )


def _add_key(keys: dict[str, PermissionKey], entry: PermissionKey) -> None:
    """Add a key, or raise ValueError when it conflicts.

    The same key may appear more than once only with equal rules.
    """
    existing = keys.get(entry.key)
    if existing is None:
        keys[entry.key] = entry
        return
    if existing != entry:
        raise ValueError(
            f"Permission key {entry.key} is defined twice with "
            f"different rules: {existing!r} and {entry!r}."
        )
