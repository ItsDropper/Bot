import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";

function fakeDb(row = null) {
  const calls = [];
  return {
    calls,
    prepare(sql) {
      const statement = {
        values: [],
        bind(...values) {
          this.values = values;
          return this;
        },
        async first() {
          calls.push({ sql, values: this.values });
          return row;
        },
        async run() {
          calls.push({ sql, values: this.values });
          return { success: true };
        },
        async all() {
          calls.push({ sql, values: this.values });
          return { results: [{ discord_id: "123456789012345678", mc_username: "Dropper", linked_at: "2026-11-18" }] };
        },
      };
      return statement;
    },
  };
}

test("rejects requests without the bot bearer token", async () => {
  const DB = fakeDb();
  const response = await worker.fetch(
    new Request("https://worker.example/v1/health"),
    { DB, BOT_API_TOKEN: "a".repeat(48) },
  );
  assert.equal(response.status, 401);
  assert.equal(DB.calls.length, 0);
});

test("authenticated health check queries D1", async () => {
  const DB = fakeDb({ ok: 1 });
  const token = "b".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/health", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.match(DB.calls[0].sql, /SELECT 1 AS ok/);
});

test("reads a Minecraft link using a bound Discord ID", async () => {
  const link = { discord_id: "123456789012345678", mc_username: "Dropper", linked_at: "2026-11-18T10:00:00" };
  const DB = fakeDb(link);
  const token = "c".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/minecraft-links/discord/123456789012345678", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { link });
  assert.deepEqual(DB.calls[0].values, ["123456789012345678"]);
});

test("does not execute SQL for malformed Discord IDs", async () => {
  const DB = fakeDb();
  const token = "d".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/minecraft-links/discord/not-an-id", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 404);
  assert.equal(DB.calls.length, 0);
});

test("returns aggregate Minecraft link statistics", async () => {
  const DB = fakeDb({ total: 7, first_linked_at: "2026-01-01", latest_linked_at: "2026-11-18" });
  const token = "e".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/minecraft-links/stats", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { stats: { total: 7, first_linked_at: "2026-01-01", latest_linked_at: "2026-11-18" } });
  assert.match(DB.calls[0].sql, /COUNT/);
});

test("lists Minecraft links with parameterized pagination", async () => {
  const DB = fakeDb({ total: 1 });
  const token = "f".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/minecraft-links?limit=25&offset=50", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.total, 1);
  assert.equal(body.links.length, 1);
  assert.deepEqual(DB.calls[0].values, [25, 50]);
  assert.deepEqual(DB.calls[1].values, []);
});

test("rejects invalid Minecraft-link pagination", async () => {
  const DB = fakeDb();
  const token = "g".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/minecraft-links?limit=100&offset=-1", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});


test("creates a support ticket using bound values", async () => {
  const DB = fakeDb({ ticket_id: "123:456", status: "open" });
  const token = "h".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/tickets", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        ticket_id: "123:456", guild_id: "123456789012345678",
        channel_id: "223456789012345678", owner_id: "323456789012345678",
        reason: "Launcher crash", created_at: "2026-11-18T10:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 201);
  assert.deepEqual(DB.calls[0].values, [
    "123:456", "123456789012345678", "223456789012345678",
    "323456789012345678", "Launcher crash", "2026-11-18T10:00:00",
  ]);
});

test("rejects malformed support ticket data before SQL", async () => {
  const DB = fakeDb();
  const token = "i".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/tickets", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ guild_id: "bad" }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("ticket statistics are scoped to one guild", async () => {
  const DB = fakeDb({ total: 9, open: 2, closed: 7 });
  const token = "j".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/tickets/stats?guild_id=123456789012345678", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { stats: { total: 9, open: 2, closed: 7 } });
  assert.deepEqual(DB.calls[0].values, ["123456789012345678"]);
});


test("assigns an open support ticket with assignment audit fields", async () => {
  const DB = fakeDb({ ticket_id: "1:2", status: "open" });
  const token = "k".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/tickets/channel/223456789012345678/assign", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        assigned_to: "423456789012345678",
        assigned_by: "323456789012345678",
        assigned_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(DB.calls[1].values, [
    "423456789012345678", "323456789012345678",
    "2026-11-18T12:00:00", "223456789012345678",
  ]);
});


test("closes an open support ticket and records the closing actor", async () => {
  const DB = fakeDb({ ticket_id: "1:2", status: "open" });
  const token = "l".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/tickets/channel/223456789012345678/close", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        closed_by: "323456789012345678",
        closed_at: "2026-11-18T13:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(DB.calls[1].values, [
    "2026-11-18T13:00:00", "323456789012345678", "223456789012345678",
  ]);
});


test("reads per-guild configuration through the authenticated API", async () => {
  const config = { guild_id: "123456789012345678", support_role_id: "423456789012345678" };
  const DB = fakeDb(config);
  const token = "m".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/guilds/123456789012345678/config", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { config });
  assert.deepEqual(DB.calls[0].values, ["123456789012345678"]);
});

test("rejects malformed guild configuration without touching D1", async () => {
  const DB = fakeDb();
  const token = "n".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/guilds/123456789012345678/config", {
      method: "PUT",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        support_role_id: "not-a-discord-id",
        updated_by: "323456789012345678",
        updated_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("appends an immutable audit event using bound values", async () => {
  const DB = fakeDb();
  const token = "o".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/audit-events", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "123456789012345678",
        actor_id: "323456789012345678",
        action: "ticket.created",
        target_id: "223456789012345678",
        details: { owner_id: "323456789012345678" },
        created_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 201);
  assert.match(DB.calls[0].sql, /^INSERT INTO audit_events/);
  assert.deepEqual(DB.calls[0].values.slice(0, 4), [
    "123456789012345678", "323456789012345678", "ticket.created", "223456789012345678",
  ]);
});

test("rejects oversized audit details before SQL", async () => {
  const DB = fakeDb();
  const token = "p".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/audit-events", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "123456789012345678",
        actor_id: "323456789012345678",
        action: "ticket.created",
        details: { content: "x".repeat(4000) },
        created_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("lists audit events with a bounded guild-scoped query", async () => {
  const DB = fakeDb();
  const token = "q".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/audit-events?guild_id=123456789012345678&limit=15", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(DB.calls[0].values, ["123456789012345678", 15, 0]);
});


test("records a release announcement with a unique guild/project/tag key", async () => {
  const DB = fakeDb();
  const token = "r".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/releases", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "123456789012345678",
        project: "launcher",
        release_tag: "v1.3.3",
        channel_id: "223456789012345678",
        message_id: "323456789012345678",
        announced_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 201);
  assert.match(DB.calls[0].sql, /^INSERT INTO release_announcements/);
  assert.deepEqual(DB.calls[0].values.slice(0, 3), [
    "123456789012345678", "launcher", "v1.3.3",
  ]);
});

test("rejects invalid release announcement metadata before SQL", async () => {
  const DB = fakeDb();
  const token = "s".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/releases", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "invalid",
        project: "launcher",
        release_tag: "v1.3.3",
        channel_id: "223456789012345678",
        message_id: "323456789012345678",
        announced_at: "2026-11-18T12:00:00",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("looks up an existing release announcement by guild, project and tag", async () => {
  const announcement = { guild_id: "123456789012345678", project: "launcher", release_tag: "v1.3.3" };
  const DB = fakeDb(announcement);
  const token = "t".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/releases/123456789012345678/launcher/v1.3.3", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { announcement });
  assert.deepEqual(DB.calls[0].values, ["123456789012345678", "launcher", "v1.3.3"]);
});


test("creates a pending moderation case with validated, bound values", async () => {
  const row = { case_id: 41, guild_id: "123456789012345678", action: "warn", status: "pending" };
  const DB = fakeDb(row);
  const token = "u".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "123456789012345678",
        target_id: "223456789012345678",
        moderator_id: "323456789012345678",
        action: "warn",
        reason: "Repeated spam",
        created_at: "2026-11-18T12:00:00Z",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), { case: row });
  assert.match(DB.calls[0].sql, /INSERT INTO moderation_cases.*RETURNING \*/);
  assert.deepEqual(DB.calls[0].values.slice(0, 5), [
    "123456789012345678", "223456789012345678",
    "323456789012345678", "warn", "Repeated spam",
  ]);
});

test("rejects invalid moderation action and oversized reason before SQL", async () => {
  const DB = fakeDb();
  const token = "v".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases", {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        guild_id: "123456789012345678",
        target_id: "223456789012345678",
        moderator_id: "323456789012345678",
        action: "delete-everything",
        reason: "x".repeat(600),
        created_at: "2026-11-18T12:00:00Z",
      }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("finalizes a moderation case with a single pending-to-applied transition", async () => {
  const row = { case_id: 41, status: "applied" };
  const DB = fakeDb(row);
  const token = "w".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases/41", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "applied", completed_at: "2026-11-18T12:01:00Z" }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { case: row });
  assert.match(DB.calls[0].sql, /UPDATE moderation_cases.*status = 'pending' RETURNING \*/);
});

test("rejects malformed moderation case completion before SQL", async () => {
  const DB = fakeDb();
  const token = "x".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases/41", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ status: "pending", completed_at: "not-a-date" }),
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});

test("lists moderation cases scoped to guild and optional target", async () => {
  const DB = fakeDb({ total: 2 });
  const token = "y".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases?guild_id=123456789012345678&target_id=223456789012345678&limit=5", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(DB.calls[0].values, ["123456789012345678", "223456789012345678", 5, 0]);
  assert.deepEqual(DB.calls[1].values, ["123456789012345678", "223456789012345678"]);
});

test("rejects unbounded moderation case pagination", async () => {
  const DB = fakeDb();
  const token = "z".repeat(48);
  const response = await worker.fetch(
    new Request("https://worker.example/v1/moderation/cases?guild_id=123456789012345678&limit=500", {
      headers: { Authorization: `Bearer ${token}` },
    }),
    { DB, BOT_API_TOKEN: token },
  );
  assert.equal(response.status, 400);
  assert.equal(DB.calls.length, 0);
});
