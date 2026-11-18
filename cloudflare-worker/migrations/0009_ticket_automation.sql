ALTER TABLE support_tickets ADD COLUMN last_activity_at TEXT;
ALTER TABLE support_tickets ADD COLUMN last_customer_message_at TEXT;
ALTER TABLE support_tickets ADD COLUMN last_staff_message_at TEXT;
ALTER TABLE support_tickets ADD COLUMN first_response_at TEXT;
ALTER TABLE support_tickets ADD COLUMN last_escalated_at TEXT;

UPDATE support_tickets SET last_activity_at = created_at WHERE last_activity_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_support_tickets_open_activity ON support_tickets(guild_id, status, last_activity_at);
