-- AUTOINCREMENT so a household id is never reused after deletion.
CREATE TABLE households (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'inactive', 'broken', 'deleted')),
    created_at     TEXT NOT NULL,
    setup_by       INTEGER,
    inactive_since TEXT,
    token_budget   INTEGER
);

CREATE TABLE chats (
    platform     TEXT NOT NULL,
    chat_id      INTEGER NOT NULL,
    household_id INTEGER NOT NULL REFERENCES households(id),
    PRIMARY KEY (platform, chat_id)
);

CREATE TABLE invites (
    code         TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT,
    household_id INTEGER REFERENCES households(id)
);

CREATE TABLE login_links (
    code         TEXT PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES households(id),
    user_id      INTEGER NOT NULL,
    expires_at   TEXT NOT NULL,
    used_at      TEXT
);
