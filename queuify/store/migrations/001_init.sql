-- Money is stored as INTEGER paise (1 rupee = 100 paise), never floats or
-- decimal rupees: those cause rounding errors when a bill is split between people.
-- Timestamps are TEXT, ISO-8601 in UTC.
-- Users are identified by their Telegram user id.

-- Household members who can use the bot.
CREATE TABLE members (
    user_id      INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    joined_at    TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- One row per placed or proposed order and its state-machine status.
CREATE TABLE orders (
    id              INTEGER PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN (
        'proposed', 'claimed', 'quoting', 'checking_out', 'placed',
        'cancelled', 'superseded', 'failed', 'unknown')),
    scope           TEXT NOT NULL CHECK (scope IN ('all', 'mine')),
    proposed_by     INTEGER NOT NULL REFERENCES members(user_id),
    proposed_at     TEXT NOT NULL,
    queue_version   INTEGER NOT NULL,  -- queue version when proposed; a newer queue means a stale bill card
    orderer         INTEGER REFERENCES members(user_id),  -- who confirmed
    bill_json       TEXT,
    swiggy_order_id TEXT,
    claimed_at      TEXT,
    placed_at       TEXT,
    error           TEXT
);
CREATE INDEX idx_orders_status ON orders(status);

-- One line per request; the active queue is the lines with status 'queued'.
CREATE TABLE queue_lines (
    id                 INTEGER PRIMARY KEY,
    spin_id            TEXT NOT NULL,
    sku_id             TEXT,
    name               TEXT NOT NULL,
    variant            TEXT,
    qty                INTEGER NOT NULL CHECK (qty > 0),
    est_unit_price_paise INTEGER,
    max_qty            INTEGER,
    original_text      TEXT,
    added_by           INTEGER NOT NULL REFERENCES members(user_id),
    added_at           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued', 'ordered', 'removed')),
    order_id           INTEGER REFERENCES orders(id)  -- set when ordered
);
CREATE INDEX idx_queue_lines_status ON queue_lines(status);

-- Who a queue line is for; several owners split its cost equally.
CREATE TABLE queue_line_owners (
    line_id INTEGER NOT NULL REFERENCES queue_lines(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES members(user_id),
    PRIMARY KEY (line_id, user_id)
);

-- Open questions to a member (pick a variant, choose scope, paste a login URL).
CREATE TABLE pending (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES members(user_id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('variant', 'scope', 'login')),
    payload_json TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT
);

-- Which queue lines a proposal covers (lines stay queued while it is open).
CREATE TABLE order_proposal_lines (
    order_id                  INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    line_id                   INTEGER NOT NULL REFERENCES queue_lines(id),
    qty_at_proposal           INTEGER NOT NULL CHECK (qty_at_proposal > 0),
    price_at_proposal_paise   INTEGER,
    PRIMARY KEY (order_id, line_id)
);

-- Snapshot of what was actually ordered, with real Swiggy prices.
CREATE TABLE order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    variant          TEXT,
    qty              INTEGER NOT NULL CHECK (qty > 0),
    line_price_paise INTEGER NOT NULL,
    spin_id          TEXT,
    sku_id           TEXT
);

-- Owners of an ordered item.
CREATE TABLE order_item_owners (
    order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES members(user_id),
    PRIMARY KEY (order_item_id, user_id)
);

-- What each person owes the orderer for an order.
CREATE TABLE order_shares (
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES members(user_id),
    amount_paise INTEGER NOT NULL,
    owed_to      INTEGER NOT NULL REFERENCES members(user_id),
    PRIMARY KEY (order_id, user_id)
);

-- Audit log of queue changes, proposals, taps and Swiggy calls.
-- actor is free text (a user id or 'system'), so it has no foreign key.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    actor        TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT
);

-- Rolling window of recent group chat, fed to the agent.
CREATE TABLE chat_memory (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,
    user_id INTEGER,
    text    TEXT NOT NULL
);

-- Small settings and state values.
CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT INTO kv (key, value) VALUES ('queue_version', '0'), ('auth_state', 'logged_out');
