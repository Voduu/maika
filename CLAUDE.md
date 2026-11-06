# CLAUDE.md

Instructions for Claude Code in this repository. The full design is in
`docs/design.md`. Read the section that matches the work before writing
code, and follow the build order in its section 7.

## Project

Maika is a personal Discord bot for a small friend server. Stack:
Python 3.14, discord.py, SQLite (WAL mode, foreign keys on), Docker on
Unraid. The test bot and the real Maika use separate tokens, channels
and database files.

## Before any feature counts as finished

Run all three, and fix every failure:

```
ruff check
ruff format
pytest
```

After a change to a shared part (the database module, permission check,
settings, registry, message pipeline), run the full suite.

## Code conventions

- PEP 8 with 79-character lines. Comments and docstrings stay within 72
  characters (ruff rule W505). Split long SQL and string literals across
  lines.
- Type hints on every function. Docstrings follow PEP 257 (tests are
  exempt).
- Named constants instead of magic numbers. Explicit code over compact
  code. Readable and explainable code is the goal.
- `# noqa: E501` is allowed only for a URL that cannot be split.
- Make only the changes that were requested. Report anything else that
  looks worth changing and wait for a decision.

## Data conventions

- Discord IDs are INTEGER columns.
- Timestamps are UTC Unix seconds.
- `guild_id = 0` means everywhere (global).
- Settings and `bot_state` values are stored as TEXT and converted by
  declared type (bool, int, float, text, time, list[int], list[str],
  json).
- Decimal SQL math multiplies by `1.0` to avoid integer division.
- Migrations are numbered files in `migrations/`. Before applying one,
  the bot makes a backup with SQLite's backup function
  (`sqlite3.Connection.backup`). A plain file copy of a live database
  is never used, because WAL mode keeps recent writes in a separate file.

## Testing

- Tests live in `tests/unit`, `tests/db`, `tests/integration` and
  `tests/replay`.
- Functions that depend on time take `now` as an argument so tests
  control the clock.
- Database tests use a fresh in-memory database built from all the
  migration files.
- A bug found later gets a failing test first, then the fix.
- The code that needs thorough tests: the permission check, migrations,
  settings validation, relationship and mood math, addressing and
  command decisions, and anything that deletes data.

## Secrets and data

Never commit `.env`, the database, backups or photos. Never print or log
the token.
