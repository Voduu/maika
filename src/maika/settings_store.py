"""Read, set and reset setting overrides in the settings table.

Defaults live in code, and the table holds only overrides
(``docs/design.md`` section 9.6). A value is looked up in this order:
the server row, the global row (``guild_id`` 0), then the default.

Every change writes its row and one audit entry (kind ``setting``) in
a single transaction. Nothing here reads the clock: the caller passes
``now`` in UTC Unix seconds.

``set_setting_value`` and ``reset_setting_value`` are the entry points
for ``/config set`` and ``/config reset``: they wrap ``set_value`` and
``reset_value`` and also report the effective values before and after.
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from maika import audit
from maika.database import GLOBAL_GUILD_ID, transaction
from maika.settings import (
    SettingDefinition,
    SettingsRegistry,
    SettingValueError,
    format_value,
    parse_value,
)

AUDIT_KIND_SETTING = "setting"

SERVER_SCOPE_RULE = "is a global setting and cannot be set for one server"

SELECT_TEXT_SQL = "SELECT value FROM settings WHERE key = ? AND guild_id = ?"

UPSERT_SQL = (
    "INSERT INTO settings (key, guild_id, value, updated_at, updated_by) "
    "VALUES (:key, :guild_id, :value, :updated_at, :updated_by) "
    "ON CONFLICT (key, guild_id) DO UPDATE SET "
    "value = excluded.value, "
    "updated_at = excluded.updated_at, "
    "updated_by = excluded.updated_by"
)

DELETE_SQL = "DELETE FROM settings WHERE key = ? AND guild_id = ?"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingChange:
    """The result of a set or reset.

    ``old_text`` and ``new_text`` are the stored text before and
    after, or None when there was or is no row. ``changed`` is False
    when nothing was written.
    """

    key: str
    guild_id: int
    old_text: str | None
    new_text: str | None
    changed: bool


@dataclass(frozen=True)
class SettingLookup:
    """A setting's typed value and where it came from.

    ``stored`` is True when a stored row (server or global) supplied
    the value, and False when the default applies.
    """

    value: Any
    stored: bool


def _read_text(
    connection: sqlite3.Connection, key: str, guild_id: int
) -> str | None:
    """Return the stored text for a key and guild, or None."""
    row = connection.execute(SELECT_TEXT_SQL, (key, guild_id)).fetchone()
    if row is None:
        return None
    return row["value"]


def _check_scope(definition: SettingDefinition, guild_id: int) -> None:
    """Refuse a server row for a setting without server scope."""
    if guild_id != GLOBAL_GUILD_ID and not definition.server_scope:
        raise SettingValueError(definition.key, SERVER_SCOPE_RULE)


def get_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> Any:
    """Return the typed value of a setting.

    The lookup rules are those of ``lookup_value``.
    """
    return lookup_value(connection, registry, key, guild_id).value


def lookup_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> SettingLookup:
    """Return the typed value of a setting and whether it was stored.

    The server row is used only when ``guild_id`` is not 0 and the
    setting allows server scope. Otherwise the global row is used,
    and with no row the definition's default (which may be None).

    Stored text that no longer parses (for example after a rule was
    tightened) logs a warning naming the key and guild, never the
    value, and the lookup continues: a bad server row falls back to
    the global row, and a bad global row to the default.

    An unknown key raises UnknownSettingError.
    """
    definition = registry.get(key)

    lookup_guilds = [GLOBAL_GUILD_ID]
    if guild_id != GLOBAL_GUILD_ID and definition.server_scope:
        lookup_guilds.insert(0, guild_id)

    for lookup_guild in lookup_guilds:
        text = _read_text(connection, key, lookup_guild)
        if text is None:
            continue
        try:
            return SettingLookup(parse_value(definition, text), True)
        except SettingValueError:
            # Keep going down the lookup order: a bad server row
            # falls back to the global row, and a bad global row to
            # the default.
            logger.warning(
                "Stored value for setting %s (guild %d) is invalid. "
                "Ignoring it.",
                key,
                lookup_guild,
            )
            continue

    return SettingLookup(definition.default, False)


def set_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    text: str,
    *,
    guild_id: int = GLOBAL_GUILD_ID,
    actor_id: int,
    now: int,
) -> SettingChange:
    """Store a new value for a setting and audit the change.

    The text is converted and checked with ``parse_value``, and a bad
    value raises SettingValueError before anything is written. A
    non-zero ``guild_id`` on a setting without server scope also
    raises SettingValueError.

    In one transaction the row is written (replacing any row for the
    same key and guild) together with one audit entry. When the new
    stored text equals the old, nothing is written and ``changed`` is
    False.

    An unknown key raises UnknownSettingError.
    """
    definition = registry.get(key)
    value = parse_value(definition, text)
    _check_scope(definition, guild_id)
    new_text = format_value(definition, value)

    with transaction(connection):
        old_text = _read_text(connection, key, guild_id)
        if new_text == old_text:
            return SettingChange(key, guild_id, old_text, new_text, False)
        connection.execute(
            UPSERT_SQL,
            {
                "key": key,
                "guild_id": guild_id,
                "value": new_text,
                "updated_at": now,
                "updated_by": actor_id,
            },
        )
        audit.write_audit_entry(
            connection,
            kind=AUDIT_KIND_SETTING,
            actor_id=actor_id,
            now=now,
            key=key,
            guild_id=guild_id,
            old_value=old_text,
            new_value=new_text,
        )
    return SettingChange(key, guild_id, old_text, new_text, True)


def reset_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    key: str,
    *,
    guild_id: int = GLOBAL_GUILD_ID,
    actor_id: int,
    now: int,
) -> SettingChange:
    """Delete a setting's row so the default applies, and audit it.

    In one transaction the row is deleted and one audit entry is
    written, with the removed text as ``old_value`` and no
    ``new_value``. With no row, nothing is written and ``changed`` is
    False. The server-scope rule is the same as ``set_value``.

    An unknown key raises UnknownSettingError.
    """
    definition = registry.get(key)
    _check_scope(definition, guild_id)

    with transaction(connection):
        old_text = _read_text(connection, key, guild_id)
        if old_text is None:
            return SettingChange(key, guild_id, None, None, False)
        connection.execute(DELETE_SQL, (key, guild_id))
        audit.write_audit_entry(
            connection,
            kind=AUDIT_KIND_SETTING,
            actor_id=actor_id,
            now=now,
            key=key,
            guild_id=guild_id,
            old_value=old_text,
            new_value=None,
        )
    return SettingChange(key, guild_id, old_text, None, True)


@dataclass(frozen=True)
class SettingWriteResult:
    """The outcome of ``set_setting_value`` or ``reset_setting_value``.

    ``changed`` is False when nothing was written. ``old_value`` and
    ``new_value`` are the effective typed values before and after
    (the stored value, or the default when no row applies).
    ``is_default`` is True when ``new_value`` equals the default.
    ``old_stored`` is True when a stored row supplied ``old_value``.
    """

    key: str
    guild_id: int
    changed: bool
    old_value: Any
    new_value: Any
    is_default: bool
    old_stored: bool


def _write_result(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    definition: SettingDefinition,
    change: SettingChange,
    old: SettingLookup,
) -> SettingWriteResult:
    """Build a SettingWriteResult from a change and the old lookup."""
    new_value = get_value(
        connection, registry, definition.key, change.guild_id
    )
    return SettingWriteResult(
        key=definition.key,
        guild_id=change.guild_id,
        changed=change.changed,
        old_value=old.value,
        new_value=new_value,
        is_default=new_value == definition.default,
        old_stored=old.stored,
    )


def set_setting_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    *,
    key: str,
    text: str,
    guild_id: int,
    actor_id: int,
    now: int,
) -> SettingWriteResult:
    """Store a setting from typed text and report the values.

    The checks and the write are those of ``set_value``: the text is
    converted by the declared type and checked against the range or
    choices, a server ``guild_id`` on a global-only setting is
    refused (both raise SettingValueError), and the row and its audit
    entry are written in one transaction. Text that stores the same as
    the current row writes nothing.

    An unknown key raises UnknownSettingError.
    """
    definition = registry.get(key)
    old = lookup_value(connection, registry, key, guild_id)
    change = set_value(
        connection,
        registry,
        key,
        text,
        guild_id=guild_id,
        actor_id=actor_id,
        now=now,
    )
    return _write_result(connection, registry, definition, change, old)


def reset_setting_value(
    connection: sqlite3.Connection,
    registry: SettingsRegistry,
    *,
    key: str,
    guild_id: int,
    actor_id: int,
    now: int,
) -> SettingWriteResult:
    """Delete a setting's row so the default applies, and report it.

    The delete and its audit entry are those of ``reset_value``, in
    one transaction. With no row, nothing is written and ``changed``
    is False. A server ``guild_id`` on a global-only setting raises
    SettingValueError.

    An unknown key raises UnknownSettingError.
    """
    definition = registry.get(key)
    old = lookup_value(connection, registry, key, guild_id)
    change = reset_value(
        connection,
        registry,
        key,
        guild_id=guild_id,
        actor_id=actor_id,
        now=now,
    )
    return _write_result(connection, registry, definition, change, old)
