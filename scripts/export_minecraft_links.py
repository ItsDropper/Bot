#!/usr/bin/env python3
"""Export Minecraft-link records from the legacy PostgreSQL database to D1 SQL."""
import argparse
import os
import re
from pathlib import Path

import psycopg2


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="minecraft_links_export.sql")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL must point to the existing PostgreSQL database")

    connection = psycopg2.connect(database_url, sslmode="require", connect_timeout=10)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT discord_id, mc_username, linked_at FROM minecraft_links ORDER BY discord_id"
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    seen_usernames = set()
    statements = [
        "-- Exported Minecraft account links. This file contains private user data.",
        "-- Do not commit or share this file.",
    ]
    for discord_id, username, linked_at in rows:
        discord_id, username = str(discord_id), str(username)
        if not re.fullmatch(r"[0-9]{15,22}", discord_id):
            raise SystemExit(f"Invalid Discord ID in legacy database: {discord_id!r}")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", username):
            raise SystemExit(f"Invalid Minecraft username in legacy database: {username!r}")
        if username.casefold() in seen_usernames:
            raise SystemExit(
                f"Case-insensitive duplicate Minecraft username: {username!r}. "
                "Resolve it in PostgreSQL before exporting."
            )
        seen_usernames.add(username.casefold())
        if hasattr(linked_at, "isoformat"):
            linked_at = linked_at.isoformat()
        statements.append(
            "INSERT INTO minecraft_links (discord_id, mc_username, linked_at) VALUES "
            f"({sql_string(discord_id)}, {sql_string(username)}, {sql_string(str(linked_at))}) "
            "ON CONFLICT(discord_id) DO UPDATE SET "
            "mc_username = excluded.mc_username, linked_at = excluded.linked_at;"
        )

    output = Path(args.output)
    output.write_text("\n".join(statements) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} Minecraft link(s) to {output.resolve()}")


if __name__ == "__main__":
    main()
