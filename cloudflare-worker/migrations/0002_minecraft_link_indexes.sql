CREATE INDEX IF NOT EXISTS idx_minecraft_links_linked_at ON minecraft_links(linked_at DESC, discord_id ASC);
