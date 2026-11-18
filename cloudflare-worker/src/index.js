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

  if (path === "/v1/minecraft-links/stats" && request.method === "GET") {
    const stats = await env.DB.prepare(
      "SELECT COUNT(*) AS total, MIN(linked_at) AS first_linked_at, MAX(linked_at) AS latest_linked_at FROM minecraft_links"
    ).first();
    return json({ stats });
  }

  if (path === "/v1/minecraft-links" && request.method === "GET") {
    const url = new URL(request.url);
    const limit = Number(url.searchParams.get("limit") || 10);
    const offset = Number(url.searchParams.get("offset") || 0);
    if (!Number.isInteger(limit) || limit < 1 || limit > 25 ||
        !Number.isInteger(offset) || offset < 0 || offset > 1000000) {
      return json({ error: "Invalid pagination parameters" }, 400);
    }
    const [rows, count] = await Promise.all([
      env.DB.prepare(
        "SELECT discord_id, mc_username, linked_at FROM minecraft_links ORDER BY linked_at DESC, discord_id ASC LIMIT ? OFFSET ?"
      ).bind(limit, offset).all(),
      env.DB.prepare("SELECT COUNT(*) AS total FROM minecraft_links").first(),
    ]);
    return json({ links: rows.results || [], total: count.total || 0 });
  }

  if (path === "/v1/tickets" && request.method === "POST") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const { ticket_id, guild_id, channel_id, owner_id, reason, created_at } = body || {};
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (typeof ticket_id !== "string" || ticket_id.length > 64 || !validId(guild_id) || !validId(channel_id) || !validId(owner_id) || typeof reason !== "string" || !reason.trim() || reason.length > 1000 || typeof created_at !== "string" || !Number.isFinite(Date.parse(created_at))) return json({ error: "Invalid ticket data" }, 400);
    try {
      await env.DB.prepare("INSERT INTO support_tickets (ticket_id, guild_id, channel_id, owner_id, reason, status, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)").bind(ticket_id, guild_id, channel_id, owner_id, reason.trim(), created_at).run();
    } catch (error) {
      if (/unique constraint failed|constraint failed/i.test(String(error?.message || error))) return json({ error: "This member already has an open ticket or the ticket already exists" }, 409);
      throw error;
    }
    const ticket = await env.DB.prepare("SELECT * FROM support_tickets WHERE ticket_id = ?").bind(ticket_id).first();
    return json({ ticket }, 201);
  }

  if (path === "/v1/tickets" && request.method === "GET") {
    const url = new URL(request.url);
    const guildId = url.searchParams.get("guild_id") || "";
    const ownerId = url.searchParams.get("owner_id") || "";
    const status = url.searchParams.get("status") || "all";
    const limit = Number(url.searchParams.get("limit") || 10);
    const offset = Number(url.searchParams.get("offset") || 0);
    const validId = value => /^[0-9]{15,22}$/.test(value);
    if (!validId(guildId) || (ownerId && !validId(ownerId)) || !["all", "open", "closed"].includes(status) || !Number.isInteger(limit) || limit < 1 || limit > 25 || !Number.isInteger(offset) || offset < 0 || offset > 1000000) return json({ error: "Invalid ticket query parameters" }, 400);
    const clauses = ["guild_id = ?"]; const values = [guildId];
    if (ownerId) { clauses.push("owner_id = ?"); values.push(ownerId); }
    if (status !== "all") { clauses.push("status = ?"); values.push(status); }
    const where = clauses.join(" AND ");
    const [rows, count] = await Promise.all([
      env.DB.prepare("SELECT * FROM support_tickets WHERE " + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?").bind(...values, limit, offset).all(),
      env.DB.prepare("SELECT COUNT(*) AS total FROM support_tickets WHERE " + where).bind(...values).first(),
    ]);
    return json({ tickets: rows.results || [], total: count.total || 0 });
  }

  if (path === "/v1/tickets/stats" && request.method === "GET") {
    const guildId = new URL(request.url).searchParams.get("guild_id") || "";
    if (!/^[0-9]{15,22}$/.test(guildId)) return json({ error: "Invalid guild ID" }, 400);
    const stats = await env.DB.prepare("SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open, SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed FROM support_tickets WHERE guild_id = ?").bind(guildId).first();
    return json({ stats: { total: stats.total || 0, open: stats.open || 0, closed: stats.closed || 0 } });
  }

  const assignTicketMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/assign$/);
  if (assignTicketMatch && request.method === "PATCH") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    if (typeof body?.assigned_to !== "string" || !/^[0-9]{15,22}$/.test(body.assigned_to) ||
        typeof body?.assigned_by !== "string" || !/^[0-9]{15,22}$/.test(body.assigned_by) ||
        typeof body?.assigned_at !== "string" || !Number.isFinite(Date.parse(body.assigned_at))) {
      return json({ error: "Invalid ticket assignment data" }, 400);
    }
    const existing = await env.DB.prepare("SELECT * FROM support_tickets WHERE channel_id = ?").bind(assignTicketMatch[1]).first();
    if (!existing) return json({ error: "Ticket not found" }, 404);
    if (existing.status !== "open") return json({ error: "Closed tickets cannot be assigned" }, 409);
    await env.DB.prepare("UPDATE support_tickets SET assigned_to = ?, assigned_by = ?, assigned_at = ? WHERE channel_id = ? AND status = 'open'").bind(body.assigned_to, body.assigned_by, body.assigned_at, assignTicketMatch[1]).run();
    const ticket = await env.DB.prepare("SELECT * FROM support_tickets WHERE channel_id = ?").bind(assignTicketMatch[1]).first();
    return json({ ticket });
  }

  const closeTicketMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/close$/);
  if (closeTicketMatch && request.method === "PATCH") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    if (typeof body?.closed_by !== "string" || !/^[0-9]{15,22}$/.test(body.closed_by) || typeof body?.closed_at !== "string" || !Number.isFinite(Date.parse(body.closed_at))) return json({ error: "Invalid ticket closure data" }, 400);
    const existing = await env.DB.prepare("SELECT * FROM support_tickets WHERE channel_id = ?").bind(closeTicketMatch[1]).first();
    if (!existing) return json({ error: "Ticket not found" }, 404);
    if (existing.status !== "open") return json({ error: "Ticket is already closed" }, 409);
    await env.DB.prepare("UPDATE support_tickets SET status = 'closed', closed_at = ?, closed_by = ? WHERE channel_id = ? AND status = 'open'").bind(body.closed_at, body.closed_by, closeTicketMatch[1]).run();
    const ticket = await env.DB.prepare("SELECT * FROM support_tickets WHERE channel_id = ?").bind(closeTicketMatch[1]).first();
    return json({ ticket });
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
