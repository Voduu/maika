# Maika

[![CI](https://github.com/Voduu/maika/actions/workflows/ci.yml/badge.svg)](https://github.com/Voduu/maika/actions/workflows/ci.yml)

Maika is a personal Discord bot for a small friend server, written in
Python with discord.py and SQLite. This project is in the design and
early build stage. The complete design is in [docs/design.md](docs/design.md).

## Features (planned)

- Permissions with four levels (Everyone, Member, Trusted, Admin) and an
  owner above them
- Reminders, feeds (FFXIV maintenance, anime schedule), and a cat photo
  command
- Memories and per-person relationships
- Mood, presence, and a sleep schedule
- Optional LLM-backed conversation with canned-reply fallback

## Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and create an application, then add a bot user.
2. Copy the bot token (Bot > Reset Token). Keep it secret.
3. Turn off Public Bot. Enable the Message Content intent when
   conversation features start.
4. Generate an invite link (OAuth2 > URL Generator, scopes `bot` and
   `applications.commands`) and add the bot to your server.

## Configuration

Copy `.env.example` to `.env` and fill in the values. `.env` is never
committed.

| Variable | Meaning |
| --- | --- |
| `ENVIRONMENT` | `test` or `prod` |
| `DISCORD_TOKEN` | Bot token |
| `OWNER_ID` | Discord user ID of the owner |
| `LISTEN_CHANNELS` | Starting list of channel IDs, comma-separated |
| `LOG_CHANNEL_ID` | Starting channel ID for `#bot-log` |
| `DATABASE_PATH` | SQLite database file |

## Run locally

```
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m maika
```

## Run in Docker

```
docker build -t maika .
docker run --env-file .env -v ./var:/app/var maika
```

## Tests and checks

```
ruff check
ruff format --check
pytest
```

## Architecture

Three layers build on each other: foundation (permissions, settings,
database, logging), behavior (relationships, mood, presence), and the
language model connection. See [docs/design.md](docs/design.md).

## License

Not yet chosen.
