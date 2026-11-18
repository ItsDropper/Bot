CREATE TABLE support_ticket_activity_new (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'internal_note',
        'priority_changed',
        'assignment_changed',
        'customer_message',
        'staff_reply',
        'automation_escalated'
    )),
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

INSERT INTO support_ticket_activity_new
    (event_id, ticket_id, guild_id, channel_id, actor_id, action, details, created_at)
SELECT event_id, ticket_id, guild_id, channel_id, actor_id, action, details, created_at
FROM support_ticket_activity;

DROP TABLE support_ticket_activity;
ALTER TABLE support_ticket_activity_new RENAME TO support_ticket_activity;

CREATE INDEX IF NOT EXISTS idx_ticket_activity_channel
    ON support_ticket_activity(guild_id, channel_id, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_activity_ticket
    ON support_ticket_activity(ticket_id, event_id DESC);
