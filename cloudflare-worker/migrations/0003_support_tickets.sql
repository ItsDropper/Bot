CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id TEXT PRIMARY KEY NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 1000),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT,
    assigned_to TEXT
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_owner_created ON support_tickets(guild_id, owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status_created ON support_tickets(guild_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_support_tickets_one_open_per_owner ON support_tickets(guild_id, owner_id) WHERE status = 'open';
