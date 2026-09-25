/**
 * Clawdbot `agent:bootstrap` hook: fetch the Remembra session brief and inject it.
 *
 * Replaces the old handler that only prepended an instruction ("run
 * remembra_recall first"), which the model could ignore and which returned
 * semantic noise. This one calls GET /api/v1/session/brief and prepends the
 * result as `_SESSION_BRIEF.md`, so the agent starts with the latest handoff,
 * its unread inbox, current status values and recent memories by time.
 *
 * Never blocks bootstrap: on any failure it prepends a short fallback note.
 */

import type { HookHandler } from "clawdbot";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

interface HookConfig {
  url: string;
  apiKey: string;
  project: string;
  agentId: string;
}

type Json = Record<string, any>;

const TIMEOUT_MS = Number(process.env.REMEMBRA_HOOK_TIMEOUT_MS || 8000);
const MAX_CHARS = 8000;

function pluginConfig(): Json {
  const path = process.env.REMEMBRA_HOOK_CLAWDBOT_CONFIG || join(homedir(), ".clawdbot", "clawdbot.json");
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    return data?.plugins?.entries?.remembra?.config ?? {};
  } catch {
    return {};
  }
}

export function normalizeProject(project: string, aliases: string | Record<string, string> | undefined): string {
  const cleaned = project.trim().split(/\s+/).filter(Boolean).join("-") || "default";
  const map: Record<string, string> = {};
  if (typeof aliases === "string") {
    for (const part of aliases.split(",")) {
      const idx = part.indexOf("=");
      if (idx < 0) continue;
      const alias = part.slice(0, idx).trim();
      const canonical = part.slice(idx + 1).trim();
      if (alias && canonical) map[alias.toLowerCase()] = canonical;
    }
  } else if (aliases) {
    for (const [alias, canonical] of Object.entries(aliases)) map[alias.toLowerCase()] = canonical;
  }
  return map[cleaned.toLowerCase()] ?? cleaned;
}

export function loadConfig(): HookConfig {
  const cfg = pluginConfig();
  const env = process.env;
  return {
    url: String(env.REMEMBRA_URL || cfg.apiUrl || "http://localhost:8787").replace(/\/+$/, ""),
    apiKey: String(env.REMEMBRA_API_KEY || cfg.apiKey || ""),
    project: normalizeProject(
      String(env.REMEMBRA_PROJECT || cfg.projectId || "default"),
      env.REMEMBRA_PROJECT_ALIASES || cfg.projectAliases,
    ),
    agentId: String(env.REMEMBRA_AGENT_ID || cfg.agentId || "clawdbot"),
  };
}

export async function fetchBrief(config: HookConfig): Promise<Json> {
  const params = new URLSearchParams({ project_id: config.project, agent_id: config.agentId, recent_n: "10" });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(`${config.url}/api/v1/session/brief?${params}`, {
      headers: { "X-API-Key": config.apiKey, Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return (await response.json()) as Json;
  } finally {
    clearTimeout(timer);
  }
}

const clip = (text: unknown, limit: number): string => {
  const flat = String(text ?? "").split(/\s+/).filter(Boolean).join(" ");
  return flat.length <= limit ? flat : `${flat.slice(0, limit - 3)}...`;
};
const day = (ts: unknown): string => String(ts ?? "").slice(0, 16).replace("T", " ");

export function formatBrief(brief: Json): string {
  const lines: string[] = [`# Remembra session brief (project: ${brief.project_id}, agent: ${brief.agent_id})`, ""];
  const handoff = brief.handoff;
  if (handoff) {
    const who = handoff.agent_id ? ` by ${handoff.agent_id}` : "";
    lines.push(`## Latest handoff (${day(handoff.created_at)}${who})`, clip(handoff.content, 2000));
  } else {
    lines.push("## Latest handoff: none stored for this project");
  }

  const inbox = brief.inbox;
  lines.push("");
  if (!inbox) {
    lines.push("## Inbox: not checked (no agent id)");
  } else if (inbox.available === false) {
    lines.push("## Inbox: unavailable on this server");
  } else {
    lines.push(`## Inbox: ${inbox.unread_count ?? 0} unread`);
    for (const item of inbox.items ?? []) {
      lines.push(
        `- [${item.inbox_id}] from ${item.from_agent} (${day(item.created_at)}): ${clip(item.subject, 120)} — ${clip(item.body_preview, 200)}`,
      );
    }
    if (inbox.unread_count) {
      lines.push("Act on these: read full bodies with remembra_inbox_get, then remembra_inbox_ack(inbox_id, result).");
    }
  }

  const status = brief.status_items ?? [];
  if (status.length) {
    lines.push("", "## Current status");
    for (const s of status) lines.push(`- ${s.key}: ${clip(s.value, 300)} (${day(s.updated_at)})`);
  }
  const recent = brief.recent ?? [];
  if (recent.length) {
    lines.push("", "## Recent memories (newest first)");
    for (const m of recent) {
      const who = m.agent_id ? ` [${m.agent_id}]` : "";
      const kind = m.memory_type ? ` (${m.memory_type})` : "";
      lines.push(`- ${day(m.created_at)}${who}${kind}: ${clip(m.content, 300)}`);
    }
  }
  const warnings = brief.warnings ?? [];
  if (warnings.length) {
    lines.push("", "## Warnings", ...warnings.map((w: string) => `- ${w}`));
  }
  const text = lines.join("\n");
  return text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS - 40)}\n... (brief truncated; call remembra_session_brief)` : text;
}

const handler: HookHandler = async (event: any) => {
  if (event.type !== "agent" || event.action !== "bootstrap") return;
  if (!event.context?.bootstrapFiles) {
    console.log("[session-recall] No bootstrapFiles in context, skipping");
    return;
  }

  const config = loadConfig();
  let content: string;
  if (!config.apiKey) {
    content = "# Remembra session brief unavailable\n\nNo Remembra API key configured. Call `remembra_session_brief` if the plugin is available.";
  } else {
    try {
      content = formatBrief(await fetchBrief(config));
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      content = `# Remembra session brief unavailable (${reason})\n\nCall \`remembra_session_brief\` before starting work.`;
    }
  }

  event.context.bootstrapFiles.unshift({ path: "_SESSION_BRIEF.md", content, source: "hook:session-recall" });
  console.log("[session-recall] Injected Remembra session brief into bootstrap");
};

export default handler;
