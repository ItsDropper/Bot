# Vanta Bot platform operations

## Runtime architecture

- `main.py` is the process entry point.
- `bot.py` owns the Discord client, legacy commands, persistent UI views, HTTP health endpoint and lifecycle hooks.
- `config/settings.py` parses process environment and validates runtime requirements without requiring secrets at import time.
- `core/database.py` is the authenticated asynchronous client for the Cloudflare Worker API.
- `core/permissions.py` centralizes support-role resolution and fails closed if D1 is unavailable.
- `core/audit.py` records attributable operational events.
- `core/moderation.py` owns moderation case validation and persistence rules.
- `cogs/moderation.py` registers moderation slash commands.
- `cogs/tickets.py` registers staff-only ticket operations.

The bot remains hosted on Render. Cloudflare Workers exposes the authenticated D1 API; the Discord bot token is never sent to Cloudflare.

## Required Render environment

- `DISCORD_BOT_TOKEN`
- `CLOUDFLARE_WORKER_URL`
- `CLOUDFLARE_BOT_API_TOKEN`
- `PORT` (optional; defaults to `3001`)

The Worker API token must match the secret configured on the Cloudflare Worker. The Worker URL must use HTTPS outside local development.

## Moderation commands

| Command | Required Discord permission | Purpose |
| --- | --- | --- |
| `/warn` | Moderate Members | Record a warning; DM delivery is best effort |
| `/timeout` | Moderate Members | Apply a timeout (1–40,320 minutes) |
| `/kick` | Kick Members | Kick a member |
| `/ban` | Ban Members | Ban a member; optionally delete up to 7 days of messages |
| `/unban` | Ban Members | Unban by Discord user ID |
| `/cases` | Moderate Members | Inspect the latest 10 cases for a member |

The bot's highest role must be above a target for timeout, kick and ban. The server owner cannot be moderated. A case is inserted as `pending` before Discord is called, then atomically finalized as `applied` or `failed`. If D1 is unavailable after Discord accepts an action, the case remains pending rather than falsely claiming it was finalized.

## Technical support intake

The public support panel uses a structured intake for launcher, mod-compatibility and bug reports. It captures Minecraft version, loader and version, Vanta Launcher version, reproduction details, and an optional HTTP(S) log or crash-report link. Unknown environment fields may be left blank.

The intake is persisted with the support ticket and displayed in its opening embed so staff can triage without repeatedly asking for basic environment details. Users are instructed to remove access tokens and private information before sharing logs. General account questions continue to use the shorter support form.

## Ticket staff commands

| Command | Access | Purpose |
| --- | --- | --- |
| `/ticketqueue` | Configured support staff or administrator | View open tickets ordered by priority, then age |
| `/ticketnote` | Configured support staff or administrator | Store a private internal note in D1; never posts the note in the public ticket channel |
| `/ticketpriority` | Configured support staff or administrator | Set low, normal, high or urgent priority and record the change atomically |
| `/ticketassign` | Configured support staff or administrator | Assign an open ticket to a member of the configured support team and record the assignment in ticket activity |
| `/ticketactivity` | Configured support staff or administrator | View staff notes and priority changes |

Ticket notes and priority changes are stored in `support_ticket_activity`; corresponding audit events are also appended to `audit_events`.

## Database migrations

Cloudflare deployment applies all pending files in `cloudflare-worker/migrations` in order. Current platform migrations include:

- `0005_platform_core.sql`: guild configuration and audit events
- `0006_release_delivery_ledger.sql`: durable release announcement records
- `0007_moderation_cases.sql`: moderation case history
- `0008_ticket_activity.sql`: ticket priorities and staff activity

Do not manually delete D1 migration records or rerun destructive schema operations. Use the deployment workflow so migration state remains tracked.

## Operational notes

- `/serverconfig` is administrator-only.
- `/botdashboard` and `/auditlog` are support-staff-only.
- `/sendsupportpanel` publishes the persistent support panel.
- `/sendapplication` publishes the persistent staff application panel.
- `/healthcheck` is administrator-only.
- CI compiles Python, imports the bot without deployment secrets, runs Python unit tests, and validates/tests the Worker.
