"""Read, set and reset setting overrides in the settings table.

Defaults live in code, and the table holds only overrides
(``docs/design.md`` section 9.6). A value is looked up in this order:
the server row, the global row (``guild_id`` 0), then the default.

Every change writes its row and one audit entry (kind ``setting``) in
a single transaction. Nothing here reads the clock: the caller passes
``now`` in UTC Unix seconds.
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
            return parse_value(definition, text)
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

    return definition.default


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
