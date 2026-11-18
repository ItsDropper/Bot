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
