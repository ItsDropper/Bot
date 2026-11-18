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

  const guildConfigMatch = path.match(/^\/v1\/guilds\/([0-9]{15,22})\/config$/);
  if (guildConfigMatch && request.method === "GET") {
    const config = await env.DB.prepare("SELECT * FROM guild_config WHERE guild_id = ?").bind(guildConfigMatch[1]).first();
    return json({ config: config || null });
  }
  if (guildConfigMatch && request.method === "PUT") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const idFields = ["support_category_id", "support_role_id", "ticket_log_channel_id", "application_result_channel_id", "launcher_announcement_channel_id", "launcher_announcement_role_id"];
    const config = {};
    for (const field of idFields) {
      const value = body?.[field];
      if (value !== undefined && value !== null && value !== "" && (typeof value !== "string" || !/^[0-9]{15,22}$/.test(value))) {
        return json({ error: `Invalid ${field}` }, 400);
      }
      config[field] = value || null;
    }
    if (typeof body?.updated_by !== "string" || !/^[0-9]{15,22}$/.test(body.updated_by) ||
        typeof body?.updated_at !== "string" || !Number.isFinite(Date.parse(body.updated_at))) {
      return json({ error: "Invalid configuration audit metadata" }, 400);
    }
    await env.DB.prepare(
      "INSERT INTO guild_config (guild_id, support_category_id, support_role_id, ticket_log_channel_id, application_result_channel_id, launcher_announcement_channel_id, launcher_announcement_role_id, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) " +
      "ON CONFLICT(guild_id) DO UPDATE SET support_category_id=excluded.support_category_id, support_role_id=excluded.support_role_id, ticket_log_channel_id=excluded.ticket_log_channel_id, application_result_channel_id=excluded.application_result_channel_id, launcher_announcement_channel_id=excluded.launcher_announcement_channel_id, launcher_announcement_role_id=excluded.launcher_announcement_role_id, updated_by=excluded.updated_by, updated_at=excluded.updated_at"
    ).bind(guildConfigMatch[1], config.support_category_id, config.support_role_id, config.ticket_log_channel_id, config.application_result_channel_id, config.launcher_announcement_channel_id, config.launcher_announcement_role_id, body.updated_by, body.updated_at).run();
    const saved = await env.DB.prepare("SELECT * FROM guild_config WHERE guild_id = ?").bind(guildConfigMatch[1]).first();
    return json({ config: saved });
  }

  const releaseMatch = path.match(/^\/v1\/releases\/([0-9]{15,22})\/([a-z][a-z0-9_-]{0,31})\/([A-Za-z0-9][A-Za-z0-9._+-]{0,79})$/);
  if (releaseMatch && request.method === "GET") {
    const announcement = await env.DB.prepare(
      "SELECT guild_id, project, release_tag, channel_id, message_id, announced_at FROM release_announcements WHERE guild_id = ? AND project = ? AND release_tag = ?"
    ).bind(releaseMatch[1], releaseMatch[2], releaseMatch[3]).first();
    return announcement ? json({ announcement }) : json({ error: "Release has not been announced" }, 404);
  }

  if (path === "/v1/releases" && request.method === "POST") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (!validId(body?.guild_id) || !validId(body?.channel_id) || !validId(body?.message_id) ||
        typeof body?.project !== "string" || !/^[a-z][a-z0-9_-]{0,31}$/.test(body.project) ||
        typeof body?.release_tag !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$/.test(body.release_tag) ||
        typeof body?.announced_at !== "string" || !Number.isFinite(Date.parse(body.announced_at))) {
      return json({ error: "Invalid release announcement record" }, 400);
    }
    try {
      await env.DB.prepare(
        "INSERT INTO release_announcements (guild_id, project, release_tag, channel_id, message_id, announced_at) VALUES (?, ?, ?, ?, ?, ?)"
      ).bind(body.guild_id, body.project, body.release_tag, body.channel_id, body.message_id, body.announced_at).run();
    } catch (error) {
      if (/unique constraint failed|constraint failed/i.test(String(error?.message || error))) {
        return json({ error: "Release has already been recorded for this guild" }, 409);
      }
      throw error;
    }
    return json({ announcement: body }, 201);
  }

  if (path === "/v1/audit-events" && request.method === "POST") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (!validId(body?.guild_id) || !validId(body?.actor_id) ||
        typeof body?.action !== "string" || !/^[a-z][a-z0-9_.-]{1,63}$/.test(body.action) ||
        (body.target_id != null && !validId(body.target_id)) ||
        typeof body?.created_at !== "string" || !Number.isFinite(Date.parse(body.created_at))) {
      return json({ error: "Invalid audit event" }, 400);
    }
    const details = body.details ?? {};
    const serialized = JSON.stringify(details);
    if (serialized.length > 3000) return json({ error: "Audit details are too large" }, 400);
    const result = await env.DB.prepare(
      "INSERT INTO audit_events (guild_id, actor_id, action, target_id, details, created_at) VALUES (?, ?, ?, ?, ?, ?)"
    ).bind(body.guild_id, body.actor_id, body.action, body.target_id || null, serialized, body.created_at).run();
    return json({ event_id: result.meta?.last_row_id ?? null }, 201);
  }

  if (path === "/v1/audit-events" && request.method === "GET") {
    const url = new URL(request.url);
    const guildId = url.searchParams.get("guild_id") || "";
    const limit = Number(url.searchParams.get("limit") || 10);
    const offset = Number(url.searchParams.get("offset") || 0);
    if (!/^[0-9]{15,22}$/.test(guildId) || !Number.isInteger(limit) || limit < 1 || limit > 25 ||
        !Number.isInteger(offset) || offset < 0 || offset > 1000000) return json({ error: "Invalid audit query" }, 400);
    const rows = await env.DB.prepare(
      "SELECT event_id, guild_id, actor_id, action, target_id, details, created_at FROM audit_events WHERE guild_id = ? ORDER BY event_id DESC LIMIT ? OFFSET ?"
    ).bind(guildId, limit, offset).all();
    return json({ events: rows.results || [] });
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
    // Open-ticket pages must be globally priority ordered before LIMIT/OFFSET;
    // sorting only in the Discord command would hide urgent tickets on later pages.
    const orderBy = status === "open"
      ? "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 ELSE 2 END ASC, created_at ASC"
      : "created_at DESC";
    const [rows, count] = await Promise.all([
      env.DB.prepare("SELECT * FROM support_tickets WHERE " + where + " ORDER BY " + orderBy + " LIMIT ? OFFSET ?").bind(...values, limit, offset).all(),
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

  const ticketActivityMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/activity$/);
  if (ticketActivityMatch && request.method === "GET") {
    const url = new URL(request.url);
    const guildId = url.searchParams.get("guild_id") || "";
    const limit = Number(url.searchParams.get("limit") || 15);
    if (!/^[0-9]{15,22}$/.test(guildId) || !Number.isInteger(limit) || limit < 1 || limit > 25) {
      return json({ error: "Invalid ticket activity query" }, 400);
    }
    const ticket = await env.DB.prepare(
      "SELECT ticket_id FROM support_tickets WHERE guild_id = ? AND channel_id = ?"
    ).bind(guildId, ticketActivityMatch[1]).first();
    if (!ticket) return json({ error: "Ticket not found" }, 404);
    const rows = await env.DB.prepare(
      "SELECT event_id, ticket_id, guild_id, channel_id, actor_id, action, details, created_at FROM support_ticket_activity WHERE guild_id = ? AND channel_id = ? ORDER BY event_id DESC LIMIT ?"
    ).bind(guildId, ticketActivityMatch[1], limit).all();
    return json({ events: rows.results || [] });
  }

  const ticketNoteMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/notes$/);
  if (ticketNoteMatch && request.method === "POST") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (!validId(body?.guild_id) || !validId(body?.actor_id) ||
        typeof body?.note !== "string" || !body.note.trim() || body.note.length > 1000 ||
        typeof body?.created_at !== "string" || !Number.isFinite(Date.parse(body.created_at))) {
      return json({ error: "Invalid internal ticket note" }, 400);
    }
    const event = await env.DB.prepare(
      "INSERT INTO support_ticket_activity (ticket_id, guild_id, channel_id, actor_id, action, details, created_at) " +
      "SELECT ticket_id, guild_id, channel_id, ?, 'internal_note', ?, ? FROM support_tickets " +
      "WHERE guild_id = ? AND channel_id = ? AND status = 'open' RETURNING *"
    ).bind(body.actor_id, JSON.stringify({ note: body.note.trim() }), body.created_at,
      body.guild_id, ticketNoteMatch[1]).first();
    if (!event) return json({ error: "Open ticket not found" }, 404);
    return json({ event }, 201);
  }

  const ticketPriorityMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/priority$/);
  if (ticketPriorityMatch && request.method === "PATCH") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (!validId(body?.guild_id) || !validId(body?.actor_id) ||
        !["low", "normal", "high", "urgent"].includes(body?.priority) ||
        typeof body?.created_at !== "string" || !Number.isFinite(Date.parse(body.created_at))) {
      return json({ error: "Invalid ticket priority update" }, 400);
    }
    const update = env.DB.prepare(
      "UPDATE support_tickets SET priority = ? WHERE guild_id = ? AND channel_id = ? AND status = 'open'"
    ).bind(body.priority, body.guild_id, ticketPriorityMatch[1]);
    const activity = env.DB.prepare(
      "INSERT INTO support_ticket_activity (ticket_id, guild_id, channel_id, actor_id, action, details, created_at) " +
      "SELECT ticket_id, guild_id, channel_id, ?, 'priority_changed', ?, ? FROM support_tickets " +
      "WHERE guild_id = ? AND channel_id = ? AND status = 'open'"
    ).bind(body.actor_id, JSON.stringify({ priority: body.priority }), body.created_at,
      body.guild_id, ticketPriorityMatch[1]);
    const results = await env.DB.batch([update, activity]);
    if (!results[0]?.meta?.changes) return json({ error: "Open ticket not found" }, 404);
    const ticket = await env.DB.prepare(
      "SELECT * FROM support_tickets WHERE guild_id = ? AND channel_id = ?"
    ).bind(body.guild_id, ticketPriorityMatch[1]).first();
    return json({ ticket });
  }

  const assignTicketMatch = path.match(/^\/v1\/tickets\/channel\/([0-9]{15,22})\/assign$/);
  if (assignTicketMatch && request.method === "PATCH") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    if (!validId(body?.guild_id) || !validId(body?.assigned_to) || !validId(body?.assigned_by) ||
        typeof body?.assigned_at !== "string" || !Number.isFinite(Date.parse(body.assigned_at))) {
      return json({ error: "Invalid ticket assignment data" }, 400);
    }
    const update = env.DB.prepare(
      "UPDATE support_tickets SET assigned_to = ?, assigned_by = ?, assigned_at = ? " +
      "WHERE guild_id = ? AND channel_id = ? AND status = 'open'"
    ).bind(body.assigned_to, body.assigned_by, body.assigned_at, body.guild_id, assignTicketMatch[1]);
    const activity = env.DB.prepare(
      "INSERT INTO support_ticket_activity (ticket_id, guild_id, channel_id, actor_id, action, details, created_at) " +
      "SELECT ticket_id, guild_id, channel_id, ?, 'assignment_changed', ?, ? FROM support_tickets " +
      "WHERE guild_id = ? AND channel_id = ? AND status = 'open'"
    ).bind(body.assigned_by, JSON.stringify({ assigned_to: body.assigned_to }), body.assigned_at,
      body.guild_id, assignTicketMatch[1]);
    const results = await env.DB.batch([update, activity]);
    if (!results[0]?.meta?.changes) return json({ error: "Open ticket not found" }, 404);
    const ticket = await env.DB.prepare(
      "SELECT * FROM support_tickets WHERE guild_id = ? AND channel_id = ?"
    ).bind(body.guild_id, assignTicketMatch[1]).first();
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

  if (path === "/v1/moderation/cases" && request.method === "POST") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    const validId = value => typeof value === "string" && /^[0-9]{15,22}$/.test(value);
    const actions = ["warn", "timeout", "kick", "ban", "unban"];
    if (!validId(body?.guild_id) || !validId(body?.target_id) || !validId(body?.moderator_id) ||
        !actions.includes(body?.action) || typeof body?.reason !== "string" ||
        !body.reason.trim() || body.reason.length > 512 ||
        typeof body?.created_at !== "string" || !Number.isFinite(Date.parse(body.created_at)) ||
        (body.expires_at != null && (typeof body.expires_at !== "string" || !Number.isFinite(Date.parse(body.expires_at))))) {
      return json({ error: "Invalid moderation case" }, 400);
    }
    const caseRow = await env.DB.prepare(
      "INSERT INTO moderation_cases (guild_id, target_id, moderator_id, action, reason, status, created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?) RETURNING *"
    ).bind(body.guild_id, body.target_id, body.moderator_id, body.action, body.reason.trim(), body.created_at, body.expires_at || null).first();
    return json({ case: caseRow }, 201);
  }

  const moderationCaseMatch = path.match(/^\/v1\/moderation\/cases\/([0-9]{1,12})$/);
  if (moderationCaseMatch && request.method === "PATCH") {
    let body;
    try { body = await readJson(request); } catch { return json({ error: "Invalid JSON request body" }, 400); }
    if (!["applied", "failed"].includes(body?.status) ||
        typeof body?.completed_at !== "string" || !Number.isFinite(Date.parse(body.completed_at)) ||
        (body.failure_reason != null && (typeof body.failure_reason !== "string" || body.failure_reason.length > 500))) {
      return json({ error: "Invalid moderation case completion" }, 400);
    }
    const caseId = Number(moderationCaseMatch[1]);
    const caseRow = await env.DB.prepare(
      "UPDATE moderation_cases SET status = ?, completed_at = ?, failure_reason = ? WHERE case_id = ? AND status = 'pending' RETURNING *"
    ).bind(body.status, body.completed_at, body.failure_reason || null, caseId).first();
    if (!caseRow) return json({ error: "Moderation case does not exist or has already been finalized" }, 409);
    return json({ case: caseRow });
  }

  if (path === "/v1/moderation/cases" && request.method === "GET") {
    const url = new URL(request.url);
    const guildId = url.searchParams.get("guild_id") || "";
    const targetId = url.searchParams.get("target_id") || "";
    const limit = Number(url.searchParams.get("limit") || 10);
    const offset = Number(url.searchParams.get("offset") || 0);
    const validId = value => /^[0-9]{15,22}$/.test(value);
    if (!validId(guildId) || (targetId && !validId(targetId)) ||
        !Number.isInteger(limit) || limit < 1 || limit > 25 ||
        !Number.isInteger(offset) || offset < 0 || offset > 1000000) {
      return json({ error: "Invalid moderation case query" }, 400);
    }
    const clauses = ["guild_id = ?"];
    const values = [guildId];
    if (targetId) { clauses.push("target_id = ?"); values.push(targetId); }
    const where = clauses.join(" AND ");
    const [rows, count] = await Promise.all([
      env.DB.prepare("SELECT * FROM moderation_cases WHERE " + where + " ORDER BY case_id DESC LIMIT ? OFFSET ?")
        .bind(...values, limit, offset).all(),
      env.DB.prepare("SELECT COUNT(*) AS total FROM moderation_cases WHERE " + where)
        .bind(...values).first(),
    ]);
    return json({ cases: rows.results || [], total: count.total || 0 });
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
