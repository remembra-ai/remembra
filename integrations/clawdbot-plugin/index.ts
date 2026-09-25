/**
 * Remembra Plugin for Clawdbot (v2)
 *
 * Shared persistent memory for Mani's agent fleet. Changes from v1 (AGT-2..12):
 *  - Session tools: remembra_session_brief, remembra_status_set / _list.
 *  - Inbox tools: remembra_inbox_get (summary mode), remembra_inbox_send, remembra_inbox_ack.
 *  - Provenance: every store is stamped {agent_id, session_id, host, client_version, source}.
 *  - remembra_forget "all" is project-scoped, dry-run by default, and needs a confirm phrase.
 *  - Fixed endpoints: /api/v1/timeline (server-side date range), spaces list (array response),
 *    ingest options nested under `options`, update reads `updated_entities`.
 *  - `autoSync` removed: v1 declared it but never read it.
 *  - Project ids go through an alias map so clawdbot/clawbot resolve to one namespace.
 */

import { Type } from "@sinclair/typebox";
import { randomUUID } from "node:crypto";
import { hostname } from "node:os";

const PLUGIN_VERSION = "2.0.0";
const TIMEOUT_MS = 30000;
const KNOWN_AGENTS = ["claude-code", "claude-desktop", "codex", "gemini", "clawdbot"];

interface RemembraConfig {
  apiUrl: string;
  apiKey: string;
  projectId?: string;
  agentId?: string;
  projectAliases?: Record<string, string>;
}

interface PluginApi {
  config: { plugins?: { entries?: { remembra?: { config?: RemembraConfig } } } };
  logger: { info: (...a: unknown[]) => void; error: (...a: unknown[]) => void; warn: (...a: unknown[]) => void };
  registerTool: (tool: unknown, options?: { optional?: boolean }) => void;
}

type Json = Record<string, any>;

const text = (payload: unknown) => ({ content: [{ type: "text", text: JSON.stringify(payload) }] });
const fail = (error: unknown) => text({ status: "error", error: error instanceof Error ? error.message : String(error) });

export function normalizeProject(project: string | undefined, aliases: Record<string, string> = {}): string {
  const cleaned = String(project ?? "").trim().split(/\s+/).filter(Boolean).join("-") || "default";
  const lowered: Record<string, string> = {};
  for (const [alias, canonical] of Object.entries(aliases)) lowered[alias.toLowerCase()] = canonical;
  return lowered[cleaned.toLowerCase()] ?? cleaned;
}

function getConfig(api: PluginApi): RemembraConfig | null {
  const cfg = api.config?.plugins?.entries?.remembra?.config;
  if (!cfg?.apiUrl || !cfg?.apiKey) return null;
  return cfg;
}

export class RemembraError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function remembraFetch(cfg: RemembraConfig, endpoint: string, method: string, body?: unknown): Promise<any> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(`${cfg.apiUrl.replace(/\/+$/, "")}${endpoint}`, {
      method,
      headers: { "Content-Type": "application/json", "X-API-Key": cfg.apiKey },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) {
      const detail = await response.text();
      throw new RemembraError(`Remembra API error: ${response.status} - ${detail.slice(0, 500)}`, response.status);
    }
    return response.status === 204 ? {} : response.json();
  } finally {
    clearTimeout(timer);
  }
}

export default function register(api: PluginApi) {
  const cfg = getConfig(api);
  if (!cfg) {
    api.logger.warn("Remembra plugin enabled but not configured (missing apiUrl/apiKey)");
    return;
  }

  const aliases = cfg.projectAliases ?? {};
  const project = normalizeProject(cfg.projectId, aliases);
  const agentId = (cfg.agentId ?? "").trim() || "clawdbot";
  const sessionId = randomUUID().replace(/-/g, "");
  const projectOf = (p?: string) => (p ? normalizeProject(p, aliases) : project);
  const provenance = () => ({
    agent_id: agentId,
    session_id: sessionId,
    host: hostname(),
    client_version: `clawdbot-plugin/${PLUGIN_VERSION}`,
    source: "clawdbot-plugin",
  });
  const call = (endpoint: string, method: string, body?: unknown) => remembraFetch(cfg, endpoint, method, body);

  if (!cfg.agentId) api.logger.warn("Remembra plugin: agentId not set; using 'clawdbot' for inbox and provenance");
  api.logger.info(`Remembra plugin ${PLUGIN_VERSION} loaded: ${cfg.apiUrl} project=${project} agent=${agentId}`);

  const tool = (spec: { name: string; description: string; parameters: unknown; run: (p: any) => Promise<unknown> }) =>
    api.registerTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      async execute(_id: string, params: any) {
        try {
          return text(await spec.run(params ?? {}));
        } catch (error) {
          return fail(error);
        }
      },
    });

  // --------------------------------------------------------------------------
  // Session start
  // --------------------------------------------------------------------------

  tool({
    name: "remembra_session_brief",
    description:
      "Call FIRST at session start: latest handoff for the project, your unread inbox, current status values, and the most recent memories by time.",
    parameters: Type.Object({
      project_id: Type.Optional(Type.String({ description: "Project (default: configured projectId)." })),
      recent_n: Type.Optional(Type.Number({ description: "Recent memories to include (0-50, default 10)." })),
    }),
    async run(p) {
      const q = new URLSearchParams({ project_id: projectOf(p.project_id), agent_id: agentId, recent_n: String(p.recent_n ?? 10) });
      return { status: "ok", ...(await call(`/api/v1/session/brief?${q}`, "GET")) };
    },
  });

  tool({
    name: "remembra_status_set",
    description:
      "Set the CURRENT value of a status key (deploy status, active sprint, blocker). Replaces the previous value instead of adding another memory.",
    parameters: Type.Object({
      key: Type.String({ description: "Status key, e.g. 'deploy:remembra-api'." }),
      value: Type.String({ description: "Current value." }),
      project_id: Type.Optional(Type.String()),
      ttl: Type.Optional(Type.String({ description: "Optional expiry, e.g. '30d'." })),
    }),
    async run(p) {
      const body: Json = { key: p.key, value: p.value, project_id: projectOf(p.project_id), metadata: provenance() };
      if (p.ttl) body.ttl = p.ttl;
      return { status: "ok", ...(await call("/api/v1/session/status", "POST", body)) };
    },
  });

  tool({
    name: "remembra_status_list",
    description: "List current status values (one per key) for a project.",
    parameters: Type.Object({ project_id: Type.Optional(Type.String()) }),
    async run(p) {
      const q = new URLSearchParams({ project_id: projectOf(p.project_id) });
      return { status: "ok", ...(await call(`/api/v1/session/status?${q}`, "GET")) };
    },
  });

  // --------------------------------------------------------------------------
  // Inbox
  // --------------------------------------------------------------------------

  tool({
    name: "remembra_inbox_get",
    description: "Read messages other agents sent to this agent (newest first). summary=true returns previews only.",
    parameters: Type.Object({
      status: Type.Optional(Type.String({ description: "'unread' (default) or 'all'." })),
      limit: Type.Optional(Type.Number({ description: "Max rows (1-200, default 20)." })),
      summary: Type.Optional(Type.Boolean({ description: "Previews only (default false)." })),
    }),
    async run(p) {
      const q = new URLSearchParams({ agent_id: agentId, status: p.status ?? "unread", limit: String(p.limit ?? 20) });
      const rows: Json[] = await call(`/api/v1/inbox?${q}`, "GET");
      const items = rows.map((r) => {
        const base: Json = { inbox_id: r.inbox_id, from_agent: r.from_agent, subject: r.subject, status: r.status, created_at: r.created_at };
        const body = String(r.body ?? "");
        return p.summary
          ? { ...base, body_preview: body.length > 200 ? `${body.slice(0, 200)}...` : body }
          : { ...base, body, metadata: r.metadata ?? {} };
      });
      return { ok: true, agent_id: agentId, count: items.length, items };
    },
  });

  tool({
    name: "remembra_inbox_send",
    description: "Send a directive to another agent's inbox (claude-code, claude-desktop, codex, gemini, clawdbot).",
    parameters: Type.Object({
      to_agent: Type.String(),
      subject: Type.String(),
      body: Type.String(),
      metadata: Type.Optional(Type.Object({})),
    }),
    async run(p) {
      const to = String(p.to_agent).trim();
      const warnings = KNOWN_AGENTS.includes(to) ? [] : [`to_agent '${to}' is not a known agent id ${JSON.stringify(KNOWN_AGENTS)}`];
      const sent = await call("/api/v1/inbox/send", "POST", {
        to_agent: to,
        from_agent: agentId,
        subject: p.subject,
        body: p.body,
        metadata: p.metadata ?? {},
      });
      return { ok: true, ...sent, to_agent: to, from_agent: agentId, warnings };
    },
  });

  tool({
    name: "remembra_inbox_ack",
    description: "Acknowledge an inbox message after acting on it. result: 'done' | 'blocked' | 'rejected' (omit = read).",
    parameters: Type.Object({
      inbox_id: Type.String(),
      result: Type.Optional(Type.String()),
      note: Type.Optional(Type.String()),
    }),
    async run(p) {
      const body: Json = {};
      if (p.result) body.result = p.result;
      if (p.note) body.note = p.note;
      return { ok: true, ...(await call(`/api/v1/inbox/${encodeURIComponent(p.inbox_id)}/ack`, "POST", body)) };
    },
  });

  // --------------------------------------------------------------------------
  // Memories
  // --------------------------------------------------------------------------

  tool({
    name: "remembra_store",
    description:
      "Store a decision, outcome or fact. memory_type 'checkpoint' = progress note that auto-expires; 'handoff' = end-of-session snapshot stored as one unit. Use remembra_status_set for changing state.",
    parameters: Type.Object({
      content: Type.String(),
      metadata: Type.Optional(Type.Object({})),
      ttl: Type.Optional(Type.String({ description: "'24h', '7d', '30d', '1y' or omit for permanent." })),
      memory_type: Type.Optional(Type.String({ description: "checkpoint | handoff | fact | observation | inference | task" })),
    }),
    async run(p) {
      if (p.memory_type === "status") return { status: "error", error: "Use remembra_status_set for status values." };
      const body: Json = {
        content: p.content,
        metadata: { ...provenance(), ...(p.metadata ?? {}) },
        project_id: project,
      };
      if (p.ttl) body.ttl = p.ttl;
      if (p.memory_type) body.memory_type = p.memory_type;
      const r = await call("/api/v1/memories", "POST", body);
      if (r.duplicate_of || !r.id) {
        return { status: "duplicate", stored: false, duplicate_of: r.duplicate_of ?? null };
      }
      return { status: "stored", stored: true, id: r.id, facts: r.extracted_facts ?? [], expires_at: r.expires_at ?? null };
    },
  });

  tool({
    name: "remembra_recall",
    description:
      "Semantic + keyword search. Use before answering about past decisions, people or projects. For 'what happened recently' use remembra_session_brief or remembra_timeline.",
    parameters: Type.Object({
      query: Type.String(),
      limit: Type.Optional(Type.Number()),
      threshold: Type.Optional(Type.Number()),
      slim: Type.Optional(Type.Boolean({ description: "Context string only (server-capped at 800 tokens)." })),
      retrieval_mode: Type.Optional(Type.String({ description: "balanced | debug (recent-first) | operational | strategic" })),
      project_id: Type.Optional(Type.String()),
    }),
    async run(p) {
      const body: Json = {
        query: p.query,
        limit: p.limit ?? 5,
        threshold: p.threshold ?? 0.4,
        project_id: projectOf(p.project_id),
      };
      if (p.slim) body.slim = true;
      if (p.retrieval_mode) body.retrieval_mode = p.retrieval_mode;
      const r = await call("/api/v1/memories/recall", "POST", body);
      const memories: Json[] = r.memories ?? [];
      if (p.slim) return { status: "ok", context: r.context, count: memories.length };
      const haystack = memories.map((m) => String(m.content ?? "").toLowerCase()).join("\n");
      const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const linked = (r.entities ?? []).filter((e: Json) => {
        const name = String(e.canonical_name ?? "").trim().toLowerCase();
        return name.length >= 2 && new RegExp(`(?<![\\w])${escape(name)}(?![\\w])`).test(haystack);
      });
      return {
        status: "ok",
        count: memories.length,
        context: r.context,
        memories: memories.map((m) => ({
          id: m.id,
          content: m.content,
          relevance: m.relevance,
          created_at: m.created_at,
          memory_type: m.memory_type ?? null,
          agent_id: m.metadata?.agent_id ?? null,
          source_id: m.metadata?.source_id ?? null,
          staleness_warning: m.staleness_warning ?? false,
        })),
        entities: linked.slice(0, 20).map((e: Json) => ({ name: e.canonical_name, type: e.type })),
      };
    },
  });

  tool({
    name: "remembra_forget",
    description:
      "Delete one memory by id. all=true wipes ONE project: requires project_id, is a dry run by default, and only deletes with dry_run=false and confirm='DELETE ALL MEMORIES IN <project_id>'.",
    parameters: Type.Object({
      memory_id: Type.Optional(Type.String()),
      entity: Type.Optional(Type.String()),
      all: Type.Optional(Type.Boolean()),
      project_id: Type.Optional(Type.String()),
      confirm: Type.Optional(Type.String()),
      dry_run: Type.Optional(Type.Boolean()),
    }),
    async run(p) {
      const targets = [p.memory_id, p.entity, p.all].filter(Boolean).length;
      if (targets !== 1) return { status: "error", error: "Specify exactly one of memory_id, entity, or all=true" };
      if (p.entity) {
        return { status: "not_supported", error: "Entity deletion is not implemented server-side; delete by memory_id." };
      }
      let r: Json;
      if (p.memory_id) {
        r = await call(`/api/v1/memories?memory_id=${encodeURIComponent(p.memory_id)}`, "DELETE");
      } else {
        if (!p.project_id || !String(p.project_id).trim()) {
          return { status: "error", error: "all=true requires an explicit project_id; user-wide wipes are not available." };
        }
        const target = projectOf(p.project_id);
        const phrase = `DELETE ALL MEMORIES IN ${target}`;
        if (p.dry_run !== false || p.confirm !== phrase) {
          const q = new URLSearchParams({ project_id: target, limit: "5", order: "desc" });
          const preview = await call(`/api/v1/timeline?${q}`, "GET");
          return {
            status: "dry_run",
            project_id: target,
            would_delete: preview.total ?? 0,
            sample: (preview.memories ?? []).map((m: Json) => ({ id: m.id, content: String(m.content ?? "").slice(0, 120) })),
            confirm_phrase: phrase,
            ...(p.dry_run === false ? { error: "Confirmation phrase missing or wrong; nothing was deleted." } : {}),
          };
        }
        r = await call(`/api/v1/memories?project_id=${encodeURIComponent(target)}`, "DELETE");
      }
      return {
        status: "deleted",
        deleted_memories: r.deleted_memories ?? 0,
        deleted_entities: r.deleted_entities ?? 0,
        deleted_relationships: r.deleted_relationships ?? 0,
      };
    },
  });

  tool({
    name: "remembra_health",
    description: "Check Remembra server health and this plugin's identity (agent id, project).",
    parameters: Type.Object({}),
    async run() {
      const health = await call("/health", "GET");
      const warnings: string[] = [];
      if (!cfg.agentId) warnings.push("agentId not configured; using 'clawdbot'.");
      if (project === "default") warnings.push("projectId is 'default'; set the shared project id.");
      return { status: "ok", server: cfg.apiUrl, agent_id: agentId, project, plugin_version: PLUGIN_VERSION, warnings, health };
    },
  });

  tool({
    name: "remembra_entities",
    description: "List/search extracted entities (people, organizations, places, concepts).",
    parameters: Type.Object({
      query: Type.Optional(Type.String()),
      type: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Number()),
    }),
    async run(p) {
      const q = new URLSearchParams({ limit: String(Math.min(p.limit ?? 20, 500)), project_id: project });
      if (p.type) q.set("entity_type", p.type);
      const r = await call(`/api/v1/entities?${q}`, "GET");
      let entities: Json[] = r.entities ?? [];
      if (p.query) {
        const needle = String(p.query).toLowerCase();
        entities = entities.filter(
          (e) =>
            String(e.canonical_name ?? "").toLowerCase().includes(needle) ||
            (e.aliases ?? []).some((a: string) => a.toLowerCase().includes(needle)),
        );
      }
      return { status: "ok", count: entities.length, entities };
    },
  });

  tool({
    name: "remembra_relationships",
    description: "Entity relationships at a point in time (e.g. where did Alice work in January 2022?).",
    parameters: Type.Object({
      entity_name: Type.String(),
      as_of: Type.Optional(Type.String()),
      relationship_type: Type.Optional(Type.String()),
      include_history: Type.Optional(Type.Boolean()),
    }),
    async run(p) {
      const q = new URLSearchParams({ entity_name: p.entity_name });
      if (p.as_of) q.set("as_of", p.as_of);
      if (p.relationship_type) q.set("relationship_type", p.relationship_type);
      if (p.include_history) q.set("include_history", "true");
      const r = await call(`/api/v1/entities/relationship-search?${q}`, "GET");
      return { status: "ok", entity: p.entity_name, as_of: p.as_of ?? "current", count: r.relationships?.length ?? 0, relationships: r.relationships ?? [] };
    },
  });

  tool({
    name: "remembra_ingest",
    description: "Ingest a conversation and extract memories (dedupe + entity extraction).",
    parameters: Type.Object({
      messages: Type.Array(
        Type.Object({
          role: Type.String(),
          content: Type.String(),
          name: Type.Optional(Type.String()),
          timestamp: Type.Optional(Type.String()),
        }),
      ),
      session_id: Type.Optional(Type.String()),
      min_importance: Type.Optional(Type.Number()),
      extract_from: Type.Optional(Type.String()),
      store: Type.Optional(Type.Boolean()),
    }),
    async run(p) {
      const r = await call("/api/v1/ingest/conversation", "POST", {
        messages: p.messages,
        session_id: p.session_id ?? sessionId,
        project_id: project,
        // v1 sent these top-level, where the API ignored them.
        options: { min_importance: p.min_importance ?? 0.5, extract_from: p.extract_from ?? "both", store: p.store !== false },
      });
      return { status: r.status, session_id: r.session_id, stats: r.stats, facts: r.facts, entities: r.entities };
    },
  });

  tool({
    name: "remembra_update",
    description: "Replace a memory's content. For changing state prefer remembra_status_set (keeps history).",
    parameters: Type.Object({
      memory_id: Type.String(),
      content: Type.String(),
      metadata: Type.Optional(Type.Object({})),
    }),
    async run(p) {
      const body: Json = { content: p.content };
      if (p.metadata) body.metadata = p.metadata;
      const r = await call(`/api/v1/memories/${encodeURIComponent(p.memory_id)}`, "PATCH", body);
      return {
        status: "updated",
        id: r.id,
        updated_entities: (r.updated_entities ?? []).map((e: Json) => ({ name: e.canonical_name, type: e.type })),
      };
    },
  });

  tool({
    name: "remembra_list",
    description: "Browse memories newest first (not semantic). Use offset/next_offset to page.",
    parameters: Type.Object({
      limit: Type.Optional(Type.Number()),
      offset: Type.Optional(Type.Number()),
      project_id: Type.Optional(Type.String()),
    }),
    async run(p) {
      const limit = Math.min(Math.max(p.limit ?? 10, 1), 50);
      const offset = Math.max(p.offset ?? 0, 0);
      const q = new URLSearchParams({ limit: String(limit), offset: String(offset), project_id: projectOf(p.project_id) });
      const rows: Json[] = await call(`/api/v1/memories?${q}`, "GET");
      return {
        status: "ok",
        count: rows.length,
        offset,
        next_offset: rows.length === limit ? offset + rows.length : null,
        memories: rows.map((m) => ({
          id: m.id,
          content: String(m.content ?? "").slice(0, 200),
          created_at: m.created_at,
          memory_type: m.memory_type ?? null,
          agent_id: m.metadata?.agent_id ?? null,
        })),
      };
    },
  });

  tool({
    name: "remembra_timeline",
    description: "Memories in time order, filtered server-side by date range (start inclusive, end exclusive) and optional exact entity.",
    parameters: Type.Object({
      entity_name: Type.Optional(Type.String()),
      start_date: Type.Optional(Type.String()),
      end_date: Type.Optional(Type.String()),
      limit: Type.Optional(Type.Number()),
      offset: Type.Optional(Type.Number()),
      order: Type.Optional(Type.String({ description: "'asc' (default) or 'desc'" })),
      project_id: Type.Optional(Type.String()),
    }),
    async run(p) {
      const q = new URLSearchParams({
        project_id: projectOf(p.project_id),
        limit: String(Math.min(Math.max(p.limit ?? 20, 1), 100)),
        offset: String(Math.max(p.offset ?? 0, 0)),
        order: p.order === "desc" ? "desc" : "asc",
      });
      if (p.start_date) q.set("start", p.start_date);
      if (p.end_date) q.set("end", p.end_date);
      if (p.entity_name) q.set("entity", p.entity_name);
      const r = await call(`/api/v1/timeline?${q}`, "GET");
      return {
        status: "ok",
        total: r.total ?? 0,
        count: r.memories?.length ?? 0,
        date_range: { start: p.start_date ?? null, end: p.end_date ?? null },
        entity_filter: p.entity_name ?? null,
        memories: r.memories ?? [],
      };
    },
  });

  // --------------------------------------------------------------------------
  // Spaces
  // --------------------------------------------------------------------------

  tool({
    name: "remembra_share",
    description: "Share a memory to a space (get ids from remembra_spaces_list).",
    parameters: Type.Object({ memory_id: Type.String(), space_id: Type.String() }),
    async run(p) {
      await call(`/api/v1/spaces/${encodeURIComponent(p.space_id)}/memories`, "POST", { memory_id: p.memory_id });
      return { status: "shared", memory_id: p.memory_id, space_id: p.space_id };
    },
  });

  tool({
    name: "remembra_spaces_list",
    description: "List memory spaces you can access.",
    parameters: Type.Object({}),
    async run() {
      const spaces: Json[] = await call("/api/v1/spaces", "GET"); // the API returns an array
      return { status: "ok", count: spaces.length, spaces };
    },
  });

  tool({
    name: "remembra_spaces_create",
    description: "Create a memory space (you get admin access).",
    parameters: Type.Object({ name: Type.String(), description: Type.Optional(Type.String()) }),
    async run(p) {
      const r = await call("/api/v1/spaces", "POST", { name: p.name, description: p.description ?? "", project_id: project });
      return { status: "created", id: r.id, name: r.name, description: r.description, owner_id: r.owner_id };
    },
  });

  tool({
    name: "remembra_spaces_recall",
    description: "Search memories in a space.",
    parameters: Type.Object({ space_id: Type.String(), query: Type.String(), limit: Type.Optional(Type.Number()) }),
    async run(p) {
      const r = await call("/api/v1/spaces/recall", "POST", { query: p.query, limit: p.limit ?? 10, space_ids: [p.space_id] });
      return { status: "ok", space_id: p.space_id, count: r.memories?.length ?? 0, memories: r.memories ?? [] };
    },
  });
}
