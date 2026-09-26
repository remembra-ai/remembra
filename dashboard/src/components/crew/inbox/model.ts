// Inbox model (spec §5.8, §9.9): pure rules behind the two visible tabs.
//
//   Needs you  "Things only a person can decide."  (audience=project)
//   Crew       "Work any agent on this crew can pick up."  (audience=crew)
//
// Each Needs-you item gets exactly one primary action, derived from the
// server's `primary_action` and `ref_type`. Server safety items sort above
// everything else and agent-originated items sort last and read
// "from cc-2 (self-declared)". Titles are server templates (ids, slugs,
// callsigns, counts), rendered as plain text all the same.

import { crewHref, type InboxScope } from '../../../lib/crew/routes';
import type { CrewEvent, InboxItemView, SessionView } from '../../../lib/crew/types';

/** An inbox item as the REST API returns it (`item_api`), plus `project_id` in "My inbox". */
export interface InboxItem extends InboxItemView {
  crew_id: string;
  safety: boolean;
  created_seq?: number | null;
  resolved_seq?: number | null;
  resolved_by?: string | null;
  created_at: string;
  updated_at: string;
  project_id?: string;
}

/** remembra.crew.schemas.SAFETY_INBOX_KINDS */
export const SAFETY_KINDS = new Set([
  'collision_escalated',
  'baton_available',
  'baton_waiting',
  'stuck_agent',
  'tamper_blocked',
  'bypass_used',
  'githook_missing',
  'false_deny_alarm',
]);

export const TAB_COPY: Record<InboxScope, { label: string; meaning: string; empty: string }> = {
  'needs-you': {
    label: 'Needs you',
    meaning: 'Things only a person can decide.',
    empty: 'Nothing needs you. The crew is running.',
  },
  crew: {
    label: 'Crew',
    meaning: 'Work any agent on this crew can pick up.',
    empty: 'No open crew work. Ready tasks and waiting batons show up here.',
  },
};

export function isSafety(item: Pick<InboxItem, 'origin' | 'kind'> & { safety?: boolean }): boolean {
  return item.safety ?? (item.origin === 'server' && SAFETY_KINDS.has(item.kind));
}

function rank(item: InboxItem): number {
  if (isSafety(item)) return 0;
  return item.origin === 'agent' ? 2 : 1;
}

/** The server order (inbox.py sort_items): safety, then server/human, then agent-originated; priority; newest first. */
export function sortItems<T extends InboxItem>(items: readonly T[]): T[] {
  return [...items].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      a.priority - b.priority ||
      (b.updated_at > a.updated_at ? 1 : b.updated_at < a.updated_at ? -1 : 0) ||
      b.id.localeCompare(a.id),
  );
}

// ---------------------------------------------------------------------------
// Primary action
// ---------------------------------------------------------------------------

export type ItemAction =
  | { type: 'confirm_decision'; label: string; decisionId: string }
  | { type: 'answer'; label: string; messageId: string; href: string }
  | { type: 'release_all'; label: string; sessionId: string }
  | { type: 'request_checkpoint'; label: string; sessionId: string }
  | { type: 'claim'; label: string }
  | { type: 'link'; label: string; href: string }
  | { type: 'none' };

/** Where an item's reference lives in the dashboard (null when there is no page for it). */
export function refHref(item: Pick<InboxItem, 'ref_type' | 'ref_id'>, project: string | null): string | null {
  if (!project || !item.ref_type) return null;
  const id = item.ref_id ?? undefined;
  switch (item.ref_type) {
    case 'task':
      return crewHref(project, 'board', { task: id });
    case 'message':
      return crewHref(project, 'channel', { thread: id });
    case 'decision':
      return crewHref(project, 'channel');
    case 'collision':
      return crewHref(project, 'feed', { feed: { types: ['collision.'] } });
    case 'zone_change':
      return crewHref(project, 'policy');
    case 'zone':
      return crewHref(project, 'zones');
    case 'claim':
    case 'session':
      return crewHref(project, 'track');
    default:
      return crewHref(project, 'track');
  }
}

/** The one primary action of an item (§9.9). Crew items: "Take it" while open. */
export function primaryAction(item: InboxItem, project: string | null): ItemAction {
  if (item.audience === 'crew') {
    if (item.state === 'open' || item.state === 'seen') return { type: 'claim', label: 'Take it' };
    const href = refHref(item, project);
    return href ? { type: 'link', label: 'Open', href } : { type: 'none' };
  }
  const ref = item.ref_id ?? '';
  switch (item.primary_action) {
    case 'confirm_decision':
      if (item.ref_type === 'decision' && ref) return { type: 'confirm_decision', label: 'Confirm', decisionId: ref };
      break;
    case 'answer':
      if (item.ref_type === 'message' && ref && project) {
        return { type: 'answer', label: 'Answer', messageId: ref, href: crewHref(project, 'channel', { thread: ref }) };
      }
      break;
    case 'release_all':
      if (item.ref_type === 'session' && ref) return { type: 'release_all', label: 'Release all claims', sessionId: ref };
      break;
    case 'checkpoint':
      if (item.ref_type === 'session' && ref) return { type: 'request_checkpoint', label: 'Request checkpoint', sessionId: ref };
      break;
    case 'review': {
      const href = refHref(item, project);
      if (href) return { type: 'link', label: item.ref_type === 'task' ? 'Review report' : 'Review', href };
      break;
    }
    case 'hand_baton':
    case 'adopt':
      if (project) return { type: 'link', label: 'Hand baton to…', href: crewHref(project, 'track') };
      break;
    case 'approve':
      if (project) return { type: 'link', label: 'Review zone change', href: crewHref(project, 'policy') };
      break;
    default:
      break;
  }
  const href = refHref(item, project);
  return href ? { type: 'link', label: 'Open', href } : { type: 'none' };
}

// ---------------------------------------------------------------------------
// Agent-originated items
// ---------------------------------------------------------------------------

/** The callsign an agent-originated title starts with ("cc-2 asked 4 questions"). */
export function titleCallsign(title: string): string | null {
  const m = /^([a-z][a-z0-9-]{0,40}-\d+)\s/i.exec(title.trim());
  return m ? m[1] : null;
}

/**
 * "from cc-2 (self-declared)" for an agent-originated item, "from cc-2" when the session is not
 * known here, null for server and human items.
 */
export function originLabel(item: Pick<InboxItem, 'origin' | 'title'>, sessions: readonly Pick<SessionView, 'callsign' | 'agent_verified'>[]): string | null {
  if (item.origin !== 'agent') return null;
  const callsign = titleCallsign(item.title);
  if (!callsign) return 'from an agent';
  const session = sessions.find((s) => s.callsign.toLowerCase() === callsign.toLowerCase());
  if (!session) return `from ${callsign}`;
  return `from ${callsign} (${session.agent_verified ? 'key-verified' : 'self-declared'})`;
}

/** "×4" when the server coalesced repeated items into one. */
export function coalescedLabel(item: Pick<InboxItem, 'coalesced_count'>): string | null {
  return item.coalesced_count > 1 ? `×${item.coalesced_count}` : null;
}

export const KIND_LABEL: Record<string, string> = {
  review_report: 'report to review',
  human_question: 'question for you',
  decision_to_confirm: 'decision to confirm',
  collision_escalated: 'collision',
  baton_available: 'baton waiting',
  baton_waiting: 'baton still waiting',
  zone_change_pending: 'zone change',
  zone_hoarding: 'zone hoarding',
  zone_contested: 'contested zone',
  stuck_agent: 'stuck agent',
  idle_park: 'idle agent',
  budget: 'budget',
  githook_missing: 'git gate missing',
  tamper_blocked: 'tamper blocked',
  bypass_used: 'bypass used',
  false_deny_alarm: 'false-deny alarm',
  report_invariant: 'report check',
  task_ready: 'ready task',
  baton_reserved: 'reserved baton',
  collision_open: 'open collision',
  task_blocked: 'blocked task',
  mention: 'mention',
  handover_offer: 'handover offer',
  override_notice: 'from Mani',
  collision_notice: 'collision',
  claim_granted: 'claim granted',
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind.replace(/_/g, ' ');
}

// ---------------------------------------------------------------------------
// Session queues ("details"): folded from the crew event log
// ---------------------------------------------------------------------------

/** How many recent crew events the session-queue view reads. */
export const QUEUE_WINDOW = 600;

/**
 * Open session-queue items from inbox events, oldest first. The REST inbox has no
 * per-session listing for humans, so the details view folds the recent event
 * log: `inbox.item_created` adds, `item_claimed` updates, `item_resolved` removes.
 */
export function foldSessionQueues(events: readonly CrewEvent[]): Map<string, InboxItemView[]> {
  const items = new Map<string, InboxItemView & { seq: number }>();
  const ordered = [...events].sort((a, b) => a.seq - b.seq);
  for (const e of ordered) {
    if (!e.type.startsWith('inbox.')) continue;
    const item = e.payload?.item as InboxItemView | undefined;
    if (!item || item.audience !== 'session' || !item.recipient) continue;
    if (['open', 'seen', 'claimed'].includes(item.state)) {
      const prev = items.get(item.id);
      items.set(item.id, { ...item, seq: prev?.seq ?? e.seq });
    } else items.delete(item.id);
  }
  const bySession = new Map<string, InboxItemView[]>();
  for (const item of [...items.values()].sort((a, b) => a.seq - b.seq)) {
    const list = bySession.get(item.recipient as string) ?? [];
    const { seq: _seq, ...view } = item;
    void _seq;
    list.push(view);
    bySession.set(item.recipient as string, list);
  }
  return bySession;
}

// ---------------------------------------------------------------------------
// Inbox URL (#/inbox?scope=…&project=…&details=…)
// ---------------------------------------------------------------------------

export type InboxDetails = 'agent' | 'sessions' | null;

/** Parameters of the old agent-inbox page; any of them opens the agent inbox under details. */
const AGENT_INBOX_PARAMS = ['compose', 'open', 'to', 'status', 'agent'];

export interface InboxRoute {
  scope: InboxScope;
  project: string | null;
  details: InboxDetails;
  alerts: boolean;
}

export function parseInboxRoute(params: URLSearchParams): InboxRoute {
  const scopeParam = params.get('scope');
  const scope: InboxScope = scopeParam === 'crew' ? 'crew' : 'needs-you';
  const detailsParam = params.get('details');
  let details: InboxDetails = detailsParam === 'agent' || detailsParam === 'sessions' ? detailsParam : null;
  if (!details && AGENT_INBOX_PARAMS.some((k) => params.has(k))) details = 'agent';
  const project = params.get('project')?.trim() || null;
  return { scope, project, details, alerts: params.get('alerts') === '1' };
}

export function inboxParams(route: Partial<InboxRoute> & { scope: InboxScope }): Record<string, string | null> {
  return {
    scope: route.scope,
    project: route.project ?? null,
    details: route.details ?? null,
    alerts: route.alerts ? '1' : null,
  };
}
