CREATE TABLE IF NOT EXISTS moderation_cases (
    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    moderator_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('warn', 'timeout', 'kick', 'ban', 'unban')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'failed')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT,
    failure_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_moderation_cases_guild_created
    ON moderation_cases(guild_id, case_id DESC);
CREATE INDEX IF NOT EXISTS idx_moderation_cases_target
    ON moderation_cases(guild_id, target_id, case_id DESC);
CREATE INDEX IF NOT EXISTS idx_moderation_cases_pending
    ON moderation_cases(status, created_at) WHERE status = 'pending';
