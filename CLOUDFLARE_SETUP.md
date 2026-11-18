# Cloudflare D1 setup for the Vanta Discord bot

The Discord bot remains a Python application hosted on **Render**. UptimeRobot continues monitoring the Render service. Cloudflare hosts only a small Worker API and the D1 database; it does not run the Discord bot or connect to the Discord Gateway.

The Worker exposes only the Minecraft-link operations the bot needs. It accesses D1 through a private binding, and the bot cannot submit arbitrary SQL.

## 1. Add GitHub repository secrets

In the Bot repository, open **Settings → Secrets and variables → Actions → New repository secret**.

Create these three secrets:

| Secret | Value |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Your Cloudflare account ID |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token described below |
| `CLOUDFLARE_BOT_API_TOKEN` | A long random secret shared only with the Worker and Render bot |
| `LEGACY_DATABASE_URL` | The old PostgreSQL database's **external** connection URL; required only to migrate existing Minecraft links |

Create an account API token in the Cloudflare dashboard. Scope it to the account that will host Vanta's database and Worker. It needs:
- **D1: Edit** to find/create the D1 database.
- **Workers Admin** at the Workers product scope for the initial creation of the new Worker. Cloudflare requires Admin access to create a Worker; after the first successful deployment, you can reduce this to **Workers Editor** for subsequent deployments.
- **Account Settings: Read** if required by Wrangler's account metadata checks.

Do not use your Global API Key. The token is used only by GitHub Actions to provision D1, apply migrations, set the Worker secret, and deploy the Worker. It is not needed by the running bot. Cloudflare documents the separate create-versus-update permission requirements in its [Workers authorization guide](https://developers.cloudflare.com/workers/authorization/).

Generate the bot-to-Worker secret using a password manager's secure random generator (at least 48 random bytes; URL-safe text is suitable). Put that value in the `CLOUDFLARE_BOT_API_TOKEN` GitHub secret. Do not put secrets in source code, workflow files, or chat.

For `LEGACY_DATABASE_URL`, copy the **External Database URL** from the existing Render PostgreSQL database (not its internal URL, because GitHub Actions runs outside Render). If you no longer need the old Minecraft-link records, omit this secret; the workflow will deploy an empty D1 database and clearly report that migration was skipped.

## 2. Run the deployment workflow

Open **Actions → Deploy Cloudflare D1 API → Run workflow** and run it on `main`.

The workflow will:
1. Find the D1 database named `vanta-discord`, creating it if it does not exist.
2. Inject its ID into Wrangler's temporary CI checkout (it does not commit generated IDs).
3. Apply pending SQL migrations to the remote D1 database.
4. Set the Worker-side `BOT_API_TOKEN` secret.
5. Deploy the `vanta-discord-db` Worker.
6. If `LEGACY_DATABASE_URL` is configured, export the existing Minecraft links from PostgreSQL, import them into D1, and print the resulting D1 row count. The export exists only in the temporary GitHub Actions runner and is never committed.
7. Print the Worker URL and Render environment-variable instructions in the workflow summary.

It is safe to run again: database creation is idempotent, and D1 migrations only apply migrations not already recorded as applied.

The workflow also runs automatically when files under `cloudflare-worker/`, the provisioning script, or this workflow change on `main`.

## 3. Configure Render

After the workflow succeeds, open its summary and copy the Worker URL. Add these environment variables to the existing **Render bot service**:

| Variable | Value |
|---|---|
| `CLOUDFLARE_WORKER_URL` | Worker URL from the Actions summary |
| `CLOUDFLARE_BOT_API_TOKEN` | The exact same random secret stored in GitHub |

Keep `DISCORD_BOT_TOKEN` and your existing Render port configuration. Do not move the bot to Cloudflare. UptimeRobot should continue monitoring the Render health endpoint, not the database Worker.

The new bot version requires the two Cloudflare variables at startup. Configure them before allowing Render to restart onto the new bot version.

## 4. Existing Minecraft-link migration

If you added `LEGACY_DATABASE_URL`, the same workflow automatically exports the existing Minecraft links from PostgreSQL and imports them into D1 after applying the schema. It prints the D1 row count in the Actions log. The export is held only in the temporary GitHub Actions runner and is never committed.

The migration uses idempotent upserts, so rerunning the workflow will update matching Discord IDs rather than create duplicate records. Keep the old PostgreSQL database until the D1 count and live bot behaviour have been verified. If you omit `LEGACY_DATABASE_URL`, migration is skipped and the new D1 database starts without old link records.

## 5. Verify in Discord

- Run `/healthcheck`; the database should report Cloudflare D1.
- Test `/mylink` and `/checklink` with a migrated account.
- Test `/link`, `/unlink`, `/forcelink`, and `/forceunlink`.
- Replace the old application panel using `/sendapplication`.

## Security and operations

- Only the Worker talks to D1. Its API exposes specific operations, not arbitrary SQL.
- The Cloudflare API token stays in GitHub Actions and is not a Render environment variable.
- The bot-to-Worker token is stored as a GitHub secret, a Worker secret, and a Render environment variable.
- Add future schema changes as numbered SQL files under `cloudflare-worker/migrations/`.
- Keep SQL exports and generated secrets out of Git.
