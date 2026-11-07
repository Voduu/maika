-- Foundation tables: design sections 9.2 to 9.8.
-- schema_migrations is created by the migration runner.

CREATE TABLE users (
    discord_id  INTEGER PRIMARY KEY,
    level       INTEGER NOT NULL CHECK (level BETWEEN 1 AND 3),
    updated_at  INTEGER NOT NULL
);

CREATE TABLE permission_overrides (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    key       TEXT NOT NULL,
    effect    TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    guild_id  INTEGER NOT NULL DEFAULT 0,
    set_by    INTEGER NOT NULL,
    set_at    INTEGER NOT NULL,
    UNIQUE (user_id, key, guild_id)
);

CREATE TABLE blocks (
    user_id   INTEGER NOT NULL,
    guild_id  INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL CHECK (kind IN ('chat', 'all')),
    set_by    INTEGER NOT NULL,
    set_at    INTEGER NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY,
    logged_at  INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    actor_id   INTEGER NOT NULL,
    target_id  INTEGER,
    key        TEXT,
    guild_id   INTEGER NOT NULL DEFAULT 0,
    old_value  TEXT,
    new_value  TEXT,
    note       TEXT
);

CREATE INDEX audit_log_target ON audit_log (target_id, logged_at);

CREATE TABLE settings (
    key         TEXT NOT NULL,
    guild_id    INTEGER NOT NULL DEFAULT 0,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  INTEGER NOT NULL,
    PRIMARY KEY (key, guild_id)
);

CREATE TABLE cooldowns (
    scope       TEXT NOT NULL,
    scope_id    INTEGER NOT NULL DEFAULT 0,
    key         TEXT NOT NULL,
    expires_at  INTEGER,
    PRIMARY KEY (scope, scope_id, key)
);

CREATE TABLE bot_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
