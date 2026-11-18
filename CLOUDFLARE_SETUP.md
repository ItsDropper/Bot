# Cloudflare D1 setup for the Vanta Discord bot

The Python bot stays on its current host (for example, Render). A small Cloudflare Worker exposes only the Minecraft-link operations the bot needs; the Worker accesses D1 through a private binding. The bot cannot submit arbitrary SQL and does not need a Cloudflare account API token.

**Deployment order matters:** the updated bot now requires both Cloudflare environment variables at startup. If your host auto-deploys from `main`, configure and deploy the Worker, then add the two bot environment variables before restarting the bot. Until then, the new bot process will exit rather than silently use the old PostgreSQL database.

## 1. Create the D1 database

Requirements: a Cloudflare account and Node.js/npm on your computer.

From the repository root:

```powershell
cd cloudflare-worker
npx wrangler login
npx wrangler d1 create vanta-discord
```

Wrangler prints a database UUID. Replace the all-zero `database_id` value in `wrangler.toml` with that UUID. Keep the binding name `DB` and database name `vanta-discord`.

## 2. Apply the schema

From `cloudflare-worker`:

```powershell
npx wrangler d1 migrations apply vanta-discord --remote
```

This creates the `minecraft_links` table with case-insensitive username uniqueness.

## 3. Create the Worker secret and deploy

Generate a strong random secret locally:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Keep the generated value safe. Set it as a Worker secret:

```powershell
npx wrangler secret put BOT_API_TOKEN
```

Paste the generated value when Wrangler prompts, then deploy:

```powershell
npx wrangler deploy
```

Copy the deployed Worker URL, for example `https://vanta-discord-db.<your-subdomain>.workers.dev`.

## 4. Migrate existing Minecraft links

Do this before removing the old PostgreSQL `DATABASE_URL`. The exporter migrates only `minecraft_links`. Tier history, tester sessions, player notes, and other tier-specific records are intentionally not migrated because the tier system is being retired.

From the repository root, install the one-time migration dependency:

```powershell
python -m pip install -r migration-requirements.txt
```

Set `DATABASE_URL` in your local PowerShell session to the existing PostgreSQL connection string (do not paste it into chat or commit it), then run:

```powershell
python scripts/export_minecraft_links.py
```

This creates `minecraft_links_export.sql` in the repository root. It validates Discord IDs and Minecraft usernames and aborts if case-insensitive duplicate Minecraft names exist. The export contains private user data; do not commit or share it.

Import it into D1:

```powershell
cd cloudflare-worker
npx wrangler d1 execute vanta-discord --remote --file=../minecraft_links_export.sql
```

Verify the count:

```powershell
npx wrangler d1 execute vanta-discord --remote --command="SELECT COUNT(*) AS links FROM minecraft_links"
```

Compare that count with the export script's reported row count. Keep the PostgreSQL database intact until the migrated links have been verified and the bot is running against D1.

## 5. Configure the bot host

In the bot host's environment settings, add:

| Variable | Value |
|---|---|
| `CLOUDFLARE_WORKER_URL` | Deployed Worker URL, without a trailing slash |
| `CLOUDFLARE_BOT_API_TOKEN` | The exact secret entered for `BOT_API_TOKEN` |
| `DISCORD_BOT_TOKEN` | Keep the existing Discord bot token |
| `PORT` | Keep the existing host-provided port configuration |

After deployment, run `/healthcheck`. Once it reports Cloudflare D1 as healthy and the migrated links work, remove the old `DATABASE_URL` from the bot host. Do not delete the old database until you have a separate backup and have completed verification.

## 6. Verify in Discord

- Run `/healthcheck`; the database field should report Cloudflare D1.
- Test `/mylink` and `/checklink` using a migrated account.
- Test `/link`, `/unlink`, `/forcelink`, and `/forceunlink`.
- Run `/sendapplication` again and replace the old application panel.
- Remove the old tier-testing panel; its buttons now return a retirement message.

## Security and operations

- Only the Worker talks to D1. Its API exposes specific operations, not arbitrary SQL.
- The bot's shared secret is a Worker secret, not a Cloudflare account API token.
- Add future schema changes as numbered SQL files under `migrations/`, then apply them with Wrangler.
- Keep the export SQL and generated secret out of Git.
