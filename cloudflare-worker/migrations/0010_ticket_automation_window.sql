ALTER TABLE support_tickets ADD COLUMN automation_started_at TEXT;

-- Existing tickets enter the automation window at migration time so the first
-- monitor pass does not immediately escalate the entire historical backlog.
UPDATE support_tickets SET automation_started_at = datetime('now') WHERE automation_started_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_support_tickets_automation_window
    ON support_tickets(guild_id, status, automation_started_at);
