// Remembra Relay API: the handoff trail, activity summary, pickup brief and
// the agent inbox. Thin typed wrappers over the authenticated API client.

import { api, ApiError } from './api';

export interface TrailCommit {
  sha: string | null;
  subject: string | null;
}

export interface TrailDetailStructured {
  structured: true;
  done: string[];
  not_done: string[];
  failing: string[];
  next: string | null;
  next_source?: string | null;
  commits: TrailCommit[];
  upstream?: string | null;
  unpushed_commits?: number | null;
  files_changed_count?: number | null;
  uncommitted_count?: number | null;
  grounding_status?: 'none' | 'consistent' | 'contradicted' | string | null;
  agent_verified?: boolean;
  end_reason?: string | null;
}

export interface TrailDetailFreeform {
  structured: false;
  content: string;
}

export type TrailDetail = TrailDetailStructured | TrailDetailFreeform;

export interface TrailItem {
  id: string;
  project_id: string | null;
  memory_type: 'handoff' | 'checkpoint' | string;
  agent_id: string | null;
  session_id: string | null;
  created_at: string;
  branch: string | null;
  head_commit: string | null;
  headline: string;
  failing: number;
  open: number;
  /** Present on servers with the dashboard read endpoints. */
  detail?: TrailDetail;
}

export interface TrailResponse {
  project_id: string | null;
  agent_id?: string | null;
  items: TrailItem[];
  /** Every match; with a cursor, the matches older than it. */
  total: number;
  /** The cursor the server paged by (absent on servers without cursor paging). */
  before?: { created_at: string; id: string | null } | null;
}

/**
 * Merge the live head page with older pages already on screen. Older pages
 * keep the entries that were in the head when they were loaded, so new
 * arrivals never push anything out of view. A re-closed session supersedes
 * its earlier handoff, so only the newest handoff per (agent, session) stays.
 */
export function mergeTrailPages(head: TrailItem[], older: TrailItem[]): TrailItem[] {
  const seen = new Set<string>();
  const sessions = new Set<string>();
  const items: TrailItem[] = [];
  for (const item of [...head, ...older]) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    if (item.memory_type === 'handoff' && item.session_id) {
      const session = `${item.agent_id ?? ''}\u0000${item.session_id}`;
      if (sessions.has(session)) continue;
      sessions.add(session);
    }
    items.push(item);
  }
  return items;
}

export interface ActivityBucket {
  handoffs: number;
  checkpoints: number;
  last_active: string | null;
  sessions_7d: number;
  daily: number[];
}

export interface AgentActivity extends ActivityBucket {
  agent_id: string;
  projects: string[];
}

export interface ProjectActivity extends ActivityBucket {
  project_id: string;
  agents: string[];
}

export interface ActivitySummary {
  generated_at: string;
  days: number;
  tz_offset_minutes: number;
  first_day: string;
  total_handoffs: number;
  total_checkpoints: number;
  week: { handoffs: number; checkpoints: number; agents: string[]; projects: string[] };
  agents: AgentActivity[];
  projects: ProjectActivity[];
}

export interface InboxMessage {
  inbox_id: string;
  from_agent: string;
  to_agent: string;
  subject: string;
  body: string;
  metadata: Record<string, unknown>;
  status: 'unread' | 'read' | 'done' | 'blocked' | 'rejected' | string;
  created_at: string;
  ack_at: string | null;
  ack_note: string | null;
  ack_result: string | null;
  expires_at: string | null;
}

export interface InboxList {
  items: InboxMessage[];
  total: number;
  status: InboxStatusFilter;
  agent_id: string | null;
}

export interface InboxAgentCounts {
  agent_id: string;
  unread: number;
  open: number;
  received: number;
  sent: number;
  last_at: string | null;
}

export interface InboxSummary {
  unread_total: number;
  open_total: number;
  agents: InboxAgentCounts[];
}

export type InboxStatusFilter = 'open' | 'unread' | 'all';
export type AckResult = 'done' | 'blocked' | 'rejected';

/** The sender id messages composed in the dashboard carry. */
export const DASHBOARD_SENDER = 'dashboard';

/** The ack note that marks a message the user withdrew before its agent picked it up. */
export const WITHDRAWN_NOTE = 'Withdrawn from the dashboard before delivery.';

/** True for a message the user withdrew (acked as done with WITHDRAWN_NOTE). */
export function isWithdrawn(message: Pick<InboxMessage, 'status' | 'ack_note'>): boolean {
  return message.status === 'done' && message.ack_note === WITHDRAWN_NOTE;
}

export interface InboxCounts {
  /** Unread messages addressed to the user (sent to the dashboard). */
  forYou: number;
  /** Unread notes waiting in agents' session briefs (not for the user). */
  pendingForAgents: number;
  /** How many agents (not the user) have at least one unread note waiting. */
  agentsWaiting: number;
}

/**
 * Splits the inbox summary into what the user has to read (the badge) and
 * what is still waiting for agents to pick up. An agent's unread note is not
 * the user's unread mail: acking it would drop it from that agent's brief.
 */
export function inboxCounts(summary: InboxSummary | null | undefined): InboxCounts {
  let forYou = 0;
  let pendingForAgents = 0;
  let agentsWaiting = 0;
  for (const agent of summary?.agents ?? []) {
    if (agent.agent_id === DASHBOARD_SENDER) {
      forYou += agent.unread;
    } else if (agent.unread > 0) {
      pendingForAgents += agent.unread;
      agentsWaiting += 1;
    }
  }
  return { forYou, pendingForAgents, agentsWaiting };
}

function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== '') search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/** Minutes east of UTC for the viewer's clock (the summary's day boundaries). */
export function localTzOffsetMinutes(): number {
  return -new Date().getTimezoneOffset();
}

export const relay = {
  trail(
    params: {
      projectId?: string | null;
      agentId?: string | null;
      limit?: number;
      offset?: number;
      /** Cursor: the oldest entry already shown; only older entries come back. */
      before?: Pick<TrailItem, 'created_at' | 'id'> | null;
    } = {},
  ) {
    return api.request<TrailResponse>(
      `/trail${query({
        project_id: params.projectId,
        agent_id: params.agentId,
        limit: params.limit ?? 30,
        offset: params.offset ?? 0,
        before: params.before?.created_at,
        before_id: params.before?.id,
      })}`,
    );
  },

  summary(days = 14) {
    return api.request<ActivitySummary>(
      `/trail/summary${query({ days, tz_offset_minutes: localTzOffsetMinutes() })}`,
    );
  },

  inboxMessages(params: { status?: InboxStatusFilter; agentId?: string | null; limit?: number; offset?: number } = {}) {
    return api.request<InboxList>(
      `/inbox/messages${query({
        status: params.status ?? 'open',
        agent_id: params.agentId,
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      })}`,
    );
  },

  inboxSummary() {
    return api.request<InboxSummary>('/inbox/summary');
  },

  sendMessage(body: { to_agent: string; subject: string; body: string; from_agent?: string }) {
    return api.request<{ inbox_id: string; status: string; created_at: string }>('/inbox/send', {
      method: 'POST',
      body: JSON.stringify({
        from_agent: DASHBOARD_SENDER,
        metadata: { source: 'dashboard' },
        ...body,
      }),
    });
  },

  ack(inboxId: string, result?: AckResult, note?: string) {
    return api.request<{ inbox_id: string; status: string; ack_at: string }>(
      `/inbox/${encodeURIComponent(inboxId)}/ack`,
      { method: 'POST', body: JSON.stringify({ result: result ?? null, note: note ?? null }) },
    );
  },
};

/**
 * A plain-language explanation of a failed call, with the fix. Pages show
 * this instead of a raw status code.
 */
export function explainError(err: unknown, what: string): { title: string; fix: string } {
  const status = err instanceof ApiError ? err.status : -1;
  const message = err instanceof Error ? err.message : String(err);
  if (status === 0) {
    return {
      title: `Couldn't reach the Remembra server to load ${what}.`,
      fix: 'Check your connection. If you self-host, make sure the API is running and the dashboard points at it (VITE_API_URL).',
    };
  }
  if (status === 401) {
    return { title: 'Your session has expired.', fix: 'Sign out and sign in again to reload your data.' };
  }
  if (status === 403) {
    return {
      title: `This key can't read ${what}.`,
      fix: 'Use a key with the viewer role or higher, or one that is not restricted to other projects (Settings, API Keys).',
    };
  }
  if (status === 404) {
    return {
      title: `This server doesn't have the ${what} API yet.`,
      fix: 'Update Remembra on the server (pip install -U remembra, or pull the latest image) and reload.',
    };
  }
  if (status === 429) {
    return { title: 'Too many requests for a moment.', fix: 'Wait a few seconds; the page retries on its own.' };
  }
  if (status === 502 || status === 504) {
    return {
      title: "The Remembra server isn't answering.",
      fix: 'It may be restarting. The page retries on its own; if you self-host, check that the API process is running.',
    };
  }
  if (status >= 500 && status !== 503) {
    const detail = message && !/^API error: \d+$/.test(message) ? ` (${message})` : '';
    return {
      title: `The server hit an error loading ${what}.`,
      fix: `The page retries every 30 seconds. If this persists, the server logs have the details${detail}.`,
    };
  }
  if (status === 503) {
    return {
      title: `${what[0].toUpperCase()}${what.slice(1)} is turned off on this server.`,
      fix: 'Ask the server operator to enable it, then reload.',
    };
  }
  return { title: `Couldn't load ${what}.`, fix: message || 'Try again in a moment.' };
}
