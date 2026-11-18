CREATE TABLE IF NOT EXISTS minecraft_links (
    discord_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(discord_id) BETWEEN 15 AND 22 AND discord_id NOT GLOB '*[^0-9]*'),
    mc_username TEXT NOT NULL COLLATE NOCASE UNIQUE
        CHECK (length(mc_username) BETWEEN 1 AND 16 AND mc_username NOT GLOB '*[^A-Za-z0-9_]*'),
    linked_at TEXT NOT NULL
);

