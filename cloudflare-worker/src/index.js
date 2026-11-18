const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
};

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

function constantTimeEqual(left, right) {
  const encoder = new TextEncoder();
  const a = encoder.encode(left);
  const b = encoder.encode(right);
  let difference = a.length ^ b.length;
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (a[index] || 0) ^ (b[index] || 0);
  }
  return difference === 0;
}

function validMinecraftUsername(value) {
  return typeof value === "string" && /^[A-Za-z0-9_]{1,16}$/.test(value);
}

async function readJson(request) {
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (contentLength > 4096) throw new Error("Request body too large");
  const raw = await request.text();
  if (raw.length > 4096) throw new Error("Request body too large");
  return JSON.parse(raw);
}

function authorized(request, env) {
  const configured = env.BOT_API_TOKEN;
  const header = request.headers.get("authorization") || "";
  if (!configured || !header.startsWith("Bearer ")) return false;
  return constantTimeEqual(header.slice(7), configured);
}

async function handle(request, env) {
  const path = new URL(request.url).pathname;

  if (path === "/health" && request.method === "GET") {
    return json({ status: "ok", service: "vanta-discord-d1" });
  }

  if (!env.DB) return json({ error: "Database binding is not configured" }, 503);
  if (!env.BOT_API_TOKEN) return json({ error: "Worker authentication is not configured" }, 503);
  if (!authorized(request, env)) return json({ error: "Unauthorized" }, 401);

  if (path === "/v1/health" && request.method === "GET") {
    await env.DB.prepare("SELECT 1 AS ok").first();
    return json({ ok: true });
  }

  const discordMatch = path.match(/^\/v1\/minecraft-links\/discord\/([0-9]{15,22})$/);
  if (discordMatch) {
    const discordId = discordMatch[1];

    if (request.method === "GET") {
      const link = await env.DB
        .prepare("SELECT discord_id, mc_username, linked_at FROM minecraft_links WHERE discord_id = ?")
        .bind(discordId).first();
      return link ? json({ link }) : json({ error: "Link not found" }, 404);
    }

    if (request.method === "PUT") {
      let body;
      try {
        body = await readJson(request);
      } catch {
        return json({ error: "Invalid JSON request body" }, 400);
      }
      const username = body.mc_username;
      const linkedAt = body.linked_at;
      if (!validMinecraftUsername(username) || typeof linkedAt !== "string" ||
          linkedAt.length > 40 || !Number.isFinite(Date.parse(linkedAt))) {
        return json({ error: "Invalid Minecraft link data" }, 400);
      }

      try {
        await env.DB.prepare(
          "INSERT INTO minecraft_links (discord_id, mc_username, linked_at) VALUES (?, ?, ?) " +
          "ON CONFLICT(discord_id) DO UPDATE SET " +
          "mc_username = excluded.mc_username, linked_at = excluded.linked_at"
        ).bind(discordId, username, linkedAt).run();
      } catch (error) {
        if (/unique constraint failed|constraint failed/i.test(String(error?.message || error))) {
          return json({ error: "Minecraft username is already linked to another Discord account" }, 409);
        }
        throw error;
      }

      const link = await env.DB
        .prepare("SELECT discord_id, mc_username, linked_at FROM minecraft_links WHERE discord_id = ?")
        .bind(discordId).first();
      return json({ link });
    }

    if (request.method === "DELETE") {
      const link = await env.DB
        .prepare("SELECT discord_id, mc_username, linked_at FROM minecraft_links WHERE discord_id = ?")
        .bind(discordId).first();
      if (!link) return json({ error: "Link not found" }, 404);
      await env.DB.prepare("DELETE FROM minecraft_links WHERE discord_id = ?").bind(discordId).run();
      return json({ link });
    }

    return json({ error: "Method not allowed" }, 405);
  }

  const usernameMatch = path.match(/^\/v1\/minecraft-links\/username\/([^/]+)$/);
  if (usernameMatch && request.method === "GET") {
    let username;
    try {
      username = decodeURIComponent(usernameMatch[1]);
    } catch {
      return json({ error: "Invalid Minecraft username" }, 400);
    }
    if (!validMinecraftUsername(username)) return json({ error: "Invalid Minecraft username" }, 400);
    const link = await env.DB
      .prepare("SELECT discord_id, mc_username, linked_at FROM minecraft_links WHERE mc_username = ? COLLATE NOCASE")
      .bind(username).first();
    return link ? json({ link }) : json({ error: "Link not found" }, 404);
  }

  return json({ error: "Not found" }, 404);
}

export default {
  async fetch(request, env) {
    try {
      return await handle(request, env);
    } catch (error) {
      console.error("Cloudflare D1 request failed:", error);
      return json({ error: "Internal database service error" }, 500);
    }
  },
};
