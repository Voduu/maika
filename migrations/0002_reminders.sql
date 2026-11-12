-- Reminders and per-person settings: design sections 9.10 and 9.11.

CREATE TABLE reminders (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    message        TEXT NOT NULL,
    due_at         INTEGER NOT NULL,
    delivery       TEXT NOT NULL CHECK (delivery IN ('dm', 'channel')),
    channel_id     INTEGER,
    created_at     INTEGER NOT NULL,
    repeat_rule    TEXT,
    timezone       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'delivered', 'failed', 'cancelled')),
    delivered_at   INTEGER,
    delivered_via  TEXT CHECK (delivered_via IN ('dm', 'channel'))
);

CREATE INDEX reminders_due ON reminders (status, due_at);

CREATE TABLE user_settings (
    user_id     INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, key)
);
