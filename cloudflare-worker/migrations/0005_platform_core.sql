CREATE TABLE IF NOT EXISTS guild_config (
    guild_id TEXT PRIMARY KEY,
    support_category_id TEXT,
    support_role_id TEXT,
    ticket_log_channel_id TEXT,
    application_result_channel_id TEXT,
    launcher_announcement_channel_id TEXT,
    launcher_announcement_role_id TEXT,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_events_guild_created
    ON audit_events(guild_id, created_at DESC, event_id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor
    ON audit_events(guild_id, actor_id, created_at DESC);
