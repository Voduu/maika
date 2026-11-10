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
| `TZ` | Time zone for local times such as `backups.time` (for example `America/Los_Angeles`). Unset uses the machine's time zone |
| `RCLONE_CONFIG` | rclone config file for the weekly offsite copy (in Docker, `/app/var/rclone/rclone.conf`) |

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

`docker stop` sends SIGTERM, and the bot shuts down cleanly: the
background tasks stop, queued log lines are sent for up to 5 seconds,
and the database closes. The image includes rclone for the weekly
offsite copy.

## Deploy on Unraid

These steps follow the design (section 5). The Unraid details (paths,
the user ID, where the template fields are) are to be verified on the
server.

1. **Build the image on the server.** From a terminal on the server:

   ```
   git clone https://github.com/Voduu/maika.git /mnt/user/appdata/maika-src
   cd /mnt/user/appdata/maika-src
   docker build -t maika .
   ```

2. **Make the appdata folder.** For example
   `/mnt/user/appdata/maika-prod`. It holds the database, the backups
   and the rclone config, and Unraid's own backup of `appdata`
   includes it. Unraid's appdata is usually owned by `99:100` (nobody
   and users); check with `ls -ln /mnt/user/appdata` and make the new
   folder match.

3. **Put the rclone config in place** at `<appdata folder>/rclone/rclone.conf`,
   made with `rclone config` on any machine or written by hand. It holds
   the storage keys, so it stays out of git. A minimal Backblaze B2
   section:

   ```
   [b2offsite]
   type = b2
   account = <application key ID>
   key = <application key>
   hard_delete = true
   ```

   The remote name in brackets must match the remote in the
   `backups.offsite_remote` setting (the default is
   `b2offsite:maika-backups`, the `maika-backups` bucket on the
   `b2offsite` remote). Create the application key in Backblaze for
   the backup bucket only, so the key cannot reach any other bucket.
   The bot also passes `--b2-hard-delete` when it deletes old copies,
   so they are removed with their old versions even without
   `hard_delete = true`.

4. **Create the container** (Docker > Add Container):
   - Repository: `maika` (the image built above).
   - Path: container path `/app/var`, host path the appdata folder.
   - Variables: every variable in `.env.example` (`ENVIRONMENT`,
     `DISCORD_TOKEN`, `OWNER_ID`, `LISTEN_CHANNELS`, `LOG_CHANNEL_ID`;
     leave `DATABASE_PATH` at its default `var/maika.db`, which is
     `/app/var/maika.db` in the container), plus
     `TZ` (for example `America/Los_Angeles`) and
     `RCLONE_CONFIG=/app/var/rclone/rclone.conf`.
   - Extra Parameters: `--user 99:100`, so the files the bot writes
     belong to the same user as the rest of appdata.

5. **Test and production are separate.** The test bot and the real
   Maika each get their own container, token, appdata folder and
   database. Never point both at the same folder.

### Update

```
cd /mnt/user/appdata/maika-src
git pull
docker build -t maika .
```

Then restart the container (or Unraid's "Force Update" if a restart
keeps the old image; to be verified). Pending database migrations run
at startup, after a backup in `backups/migrations/`.

### Restore from a backup

1. Stop the container.
2. Copy the chosen backup over the database, for example
   `cp backups/daily/maika-2026-11-09.db maika.db` in the appdata
   folder. Delete `maika.db-wal` and `maika.db-shm` if they are there,
   so the old write-ahead log is not applied to the restored file.
3. Start the container.

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

MIT. See [LICENSE](LICENSE).