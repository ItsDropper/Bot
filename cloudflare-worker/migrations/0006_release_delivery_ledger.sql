CREATE TABLE IF NOT EXISTS release_announcements (
    guild_id TEXT NOT NULL,
    project TEXT NOT NULL,
    release_tag TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    announced_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, project, release_tag)
);
CREATE INDEX IF NOT EXISTS idx_release_announcements_recent
    ON release_announcements(guild_id, announced_at DESC);
