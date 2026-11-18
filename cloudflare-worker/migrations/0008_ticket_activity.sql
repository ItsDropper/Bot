ALTER TABLE support_tickets
    ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'
    CHECK (priority IN ('low', 'normal', 'high', 'urgent'));

CREATE TABLE IF NOT EXISTS support_ticket_activity (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('internal_note', 'priority_changed')),
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ticket_activity_channel
    ON support_ticket_activity(guild_id, channel_id, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_activity_ticket
    ON support_ticket_activity(ticket_id, event_id DESC);
