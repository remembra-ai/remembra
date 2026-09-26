// Pure view model for a Mission Control lane (spec §9.3 "CrewLane anatomy").
//
// Everything a lane shows is derived here from the reducer state, the
// ephemeral presence overlay and the lane's recent events, so the wording and
// the rules (which glyph, which layer is missing, when a checkpoint is late)
// are tested without a browser. Text built here uses ids, slugs and callsigns
// only; titles are returned separately and rendered as plain text (§9).

import { beforeWriteLabel, type BeforeWrite } from '../../../lib/crew/selectors';
import { hrefFor } from '../../../lib/nav';
import { parseServerTime } from '../../../lib/time';
import type { ClaimView, CrewState, GithookState, LimitView, SessionState, TaskView, ZoneView } from '../../../lib/crew/types';

// ---------------------------------------------------------------------------
// Presence (§9.3 item 2). Status never depends on colour alone: every state
// has a glyph and a text label.
// ---------------------------------------------------------------------------

export type PulseKind = 'ring' | 'solid' | 'hollow' | 'dashed' | 'paused' | 'dropped' | 'joining' | 'ended';

export interface PresenceView {
  kind: PulseKind;
  /** Short label next to the pulse: "active", "quiet 6m", "credits ran out". */
  label: string;
  /** Second line of detail: "host offline", "billing_error · reported". */
  detail: string | null;
  stuck: boolean;
  /** A claim of this session is past its lease horizon ("lease unconfirmed"). */
  fenced: boolean;
  /** The lane has settled (quota, lost, ended): pulse stops, dashed outline. */
  settled: boolean;
}

const QUOTA_WORDS: Record<string, string> = {
  billing_error: 'credits ran out',
  rate_limit: 'rate limited',
  authentication_failed: 'signed out',
  oauth_org_not_allowed: 'org not allowed',
};

/** Minutes between an ISO time and now, floored at 0 (null when unknown). */
export function minutesAgo(iso: string | null | undefined, nowMs: number): number | null {
  const at = parseServerTime(iso);
  if (!at) return null;
  return Math.max(0, Math.floor((nowMs - at.getTime()) / 60000));
}

/** "40s", "6m", "2h", "3d" for an age in seconds. */
export function shortAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

export function sessionClaims(state: CrewState, sessionId: string): ClaimView[] {
  return Object.values(state.claims)
    .filter((c) => c.holder_kind === 'session' && c.holder_session_id === sessionId)
    .sort((a, b) => a.id.localeCompare(b.id));
}

export function presenceView(session: SessionState, claims: ClaimView[], nowMs: number, quotaSource: string | null = null): PresenceView {
  // The server's session state is authoritative (§10.1): it moves with every session event. The
  // presence overlay is a 5-second snapshot that stops arriving when an agent stops, so it never
  // decides whether a lane is active, quiet, stuck or settled (it only adds the last action).
  const state = session.state;
  const stuck = session.stuck;
  const fenced = claims.some((c) => c.fenced && (c.state === 'active' || c.state === 'offered'));
  const idleMin = minutesAgo(session.last_activity_at, nowMs);
  switch (state) {
    case 'active':
      return { kind: 'ring', label: 'active', detail: null, stuck, fenced, settled: false };
    case 'joining':
      return { kind: 'joining', label: 'joining', detail: null, stuck, fenced, settled: false };
    case 'idle':
      return {
        kind: 'solid',
        label: idleMin !== null && idleMin >= 1 ? `idle ${shortAge(idleMin * 60)}` : 'idle',
        detail: null,
        stuck,
        fenced,
        settled: false,
      };
    case 'quiet': {
      const reason =
        session.quiet_reason === 'host_unreachable' ? 'host offline' : session.quiet_reason === 'mcp_silent' ? 'no MCP calls' : null;
      return {
        kind: 'hollow',
        label: idleMin !== null ? `quiet ${shortAge(idleMin * 60)}` : 'quiet',
        detail: reason,
        stuck,
        fenced,
        settled: false,
      };
    }
    case 'quota_blocked': {
      const error = session.state_reason ?? null;
      const words = (error && QUOTA_WORDS[error]) || 'credits ran out';
      const source = quotaSource ?? (session.limit?.level === 'exhausted' ? session.limit.source : null);
      const detail = [error, source].filter(Boolean).join(' · ') || null;
      return { kind: 'dropped', label: words, detail, stuck, fenced, settled: true };
    }
    case 'lost': {
      const detail = session.state_reason === 'process_exited' ? 'process exited' : (session.state_reason ?? 'no signal');
      return { kind: 'dashed', label: 'lost', detail, stuck, fenced, settled: true };
    }
    case 'paused':
      return { kind: 'paused', label: 'paused', detail: 'by a human', stuck, fenced, settled: false };
    case 'ended':
      return { kind: 'ended', label: 'ended', detail: session.end_reason ?? null, stuck: false, fenced: false, settled: true };
  }
}

// ---------------------------------------------------------------------------
// Enforcement layers (§8.5, §9.3 item 1): "before write: enforced · commit ✓ · push ✓".
// ---------------------------------------------------------------------------

export type LayerState = 'ok' | 'missing' | 'unknown';

export interface EnforcementView {
  beforeWrite: BeforeWrite;
  commit: LayerState;
  push: LayerState;
  /** A git gate is missing: shown in the alarm style. */
  alarm: boolean;
  text: string;
}

function gitLayer(state: GithookState | null | undefined): LayerState {
  if (state === 'ok' || state === 'chained') return 'ok';
  if (state === 'missing') return 'missing';
  return 'unknown';
}

const LAYER_MARK: Record<LayerState, string> = { ok: '✓', missing: 'missing', unknown: '?' };

export function enforcementView(
  session: Pick<SessionState, 'adapter_enforcement' | 'githook_state'> & Partial<Pick<SessionState, 'client_kind'>>,
): EnforcementView {
  const beforeWrite = beforeWriteLabel(session);
  const commit = gitLayer(session.githook_state);
  const push = commit; // one git-hook state covers the pre-commit and pre-push gates (§8.4)
  if (commit === 'missing') {
    return { beforeWrite, commit, push, alarm: true, text: `before write: ${beforeWrite} · commit gate: missing` };
  }
  return {
    beforeWrite,
    commit,
    push,
    alarm: false,
    text: `before write: ${beforeWrite} · commit ${LAYER_MARK[commit]} · push ${LAYER_MARK[push]}`,
  };
}

// ---------------------------------------------------------------------------
// Zone chips (§9.3 item 3): solid exclusive, striped shared, outline watch,
// ✦ inherited (adopted or handed over).
// ---------------------------------------------------------------------------

export type ChipStyle = 'solid' | 'striped' | 'outline';

export interface ZoneChipView {
  claimId: string;
  label: string;
  /** Untrusted zone title (plain text only). */
  title: string | null;
  style: ChipStyle;
  mode: ClaimView['mode'];
  inherited: boolean;
  /** waiting in the queue for this zone */
  waiting: boolean;
  reserved: boolean;
  offered: boolean;
  fenced: boolean;
  unconfirmed: boolean;
  /** Screen-reader description: "zone pos, exclusive, inherited". */
  description: string;
}

const INHERITED_SOURCES = new Set(['adopt', 'handover']);

function claimLabel(state: CrewState, claim: ClaimView): { label: string; zone: ZoneView | null } {
  const zone = claim.zone_id ? (state.zones[claim.zone_id] ?? null) : null;
  return { label: zone?.slug ?? claim.resource ?? claim.path_glob ?? claim.id, zone };
}

export function zoneChips(state: CrewState, claims: ClaimView[]): ZoneChipView[] {
  return claims
    .filter((c) => ['active', 'offered', 'queued', 'requested', 'reserved'].includes(c.state))
    .map((claim) => {
      const { label, zone } = claimLabel(state, claim);
      const style: ChipStyle = claim.mode === 'exclusive' ? 'solid' : claim.mode === 'shared' ? 'striped' : 'outline';
      // adopted, handed over, or handed to it by a human from the dashboard
      const inherited = INHERITED_SOURCES.has(claim.source) || (claim.source === 'dashboard' && claim.holder_kind === 'session');
      const waiting = claim.state === 'queued' || claim.state === 'requested';
      const parts = [`zone ${label}`, claim.mode];
      if (inherited) parts.push('inherited');
      if (waiting) parts.push('waiting');
      if (claim.state === 'reserved') parts.push('reserved');
      if (claim.state === 'offered') parts.push('handover offered');
      if (claim.fenced) parts.push('lease unconfirmed');
      if (claim.unconfirmed) parts.push('unconfirmed');
      return {
        claimId: claim.id,
        label,
        title: zone?.title ?? null,
        style,
        mode: claim.mode,
        inherited,
        waiting,
        reserved: claim.state === 'reserved',
        offered: claim.state === 'offered',
        fenced: claim.fenced,
        unconfirmed: claim.unconfirmed,
        description: parts.join(', '),
      };
    })
    .sort((a, b) => Number(a.waiting) - Number(b.waiting) || a.label.localeCompare(b.label));
}

// ---------------------------------------------------------------------------
// Now line (§9.3 item 3): task id and title, last action with path and age.
// ---------------------------------------------------------------------------

export interface NowLine {
  taskRef: string | null;
  /** Untrusted task title (plain text). */
  taskTitle: string | null;
  task: TaskView | null;
  /** `stale`: no presence frame for more than two intervals (the agent stopped acting): shown dimmed. */
  action: { tool: string; path: string | null; ageS: number; stale: boolean } | null;
}

/** crewd sends a presence frame at most every 5 s, and only while the agent acts. */
export const PRESENCE_INTERVAL_MS = 5000;

/**
 * `frameAgeMs`: how long ago (client clock) the presence frame carrying `last_action` arrived,
 * from the store's per-session receipt time. The action's age keeps counting between frames and
 * after the last one, so an idle agent never reads "just now".
 */
export function nowLine(state: CrewState, session: SessionState, frameAgeMs: number): NowLine {
  const task = session.current_task_id ? (state.tasks[session.current_task_id] ?? null) : null;
  const action = session.presence?.last_action ?? null;
  const frameAge = Math.max(0, Number.isFinite(frameAgeMs) ? frameAgeMs : 0);
  return {
    taskRef: task ? `T-${task.number}` : (session.current_task_id ?? null),
    taskTitle: task?.title ?? null,
    task,
    action: action
      ? {
          tool: action.tool,
          path: action.path_rel ?? action.verb ?? null,
          ageS: Math.max(0, action.age_s + Math.round(frameAge / 1000)),
          stale: frameAge > 2 * PRESENCE_INTERVAL_MS,
        }
      : null,
  };
}

// ---------------------------------------------------------------------------
// Limit meter (§9.3 item 6).
// ---------------------------------------------------------------------------

export interface LimitMeterView {
  level: LimitView['level'];
  /** 0..1, null when only a level is known. */
  pct: number | null;
  source: LimitView['source'];
  text: string;
  alarm: boolean;
}

export function limitMeter(session: SessionState): LimitMeterView | null {
  const limit = session.presence?.limit ?? session.limit ?? null;
  if (!limit) return null;
  const pct = typeof limit.pct === 'number' && Number.isFinite(limit.pct) ? Math.min(1, Math.max(0, limit.pct)) : null;
  const pctText = pct !== null ? `${Math.round(pct * 100)}%` : limit.level;
  return {
    level: limit.level,
    pct,
    source: limit.source,
    text: `limit ${pctText} · ${limit.source}`,
    alarm: limit.level === 'critical' || limit.level === 'exhausted',
  };
}

// ---------------------------------------------------------------------------
// Report ring and streak (§9.3 item 5): how close the lane is to its next
// checkpoint (by calls and by time, whichever is nearer), and how many
// checkpoints in a row arrived on time.
// ---------------------------------------------------------------------------

export const CHECKPOINT_CALLS = 40;
export const CHECKPOINT_INTERVAL_S = 600;

export interface ReportRingView {
  /** 0..1 of the way to the next checkpoint. */
  fraction: number;
  calls: number;
  /** Seconds until due (negative when late); null when unknown. */
  dueInS: number | null;
  /** More than 2x overdue: the `checkpoint.missed` threshold (§5.5). */
  missed: boolean;
  label: string;
}

export function reportRing(session: SessionState, nowMs: number, lastCheckpointMs: number | null, settled = false): ReportRingView {
  const calls = session.presence?.calls_since_checkpoint ?? 0;
  const due = parseServerTime(session.presence?.next_checkpoint_due_at ?? null);
  const byCalls = Math.min(1, calls / CHECKPOINT_CALLS);
  let byTime = 0;
  let dueInS: number | null = null;
  if (due) {
    dueInS = Math.round((due.getTime() - nowMs) / 1000);
    byTime = Math.min(1, Math.max(0, 1 - dueInS / CHECKPOINT_INTERVAL_S));
  } else if (lastCheckpointMs !== null) {
    const since = (nowMs - lastCheckpointMs) / 1000;
    dueInS = Math.round(CHECKPOINT_INTERVAL_S - since);
    byTime = Math.min(1, Math.max(0, since / CHECKPOINT_INTERVAL_S));
  }
  if (settled) {
    // a stopped lane is not due anything: say when it last checkpointed
    const label = lastCheckpointMs !== null ? `stopped · last checkpoint ${shortAge((nowMs - lastCheckpointMs) / 1000)} ago` : 'stopped';
    return { fraction: 0, calls, dueInS: null, missed: false, label };
  }
  const missed = dueInS !== null && dueInS < -CHECKPOINT_INTERVAL_S;
  let label: string;
  if (missed) label = 'checkpoint overdue';
  else if (dueInS !== null && dueInS <= 0) label = 'checkpoint due';
  else if (dueInS !== null) label = `next checkpoint in ${shortAge(dueInS)}`;
  else label = `${calls} calls since checkpoint`;
  return { fraction: Math.max(byCalls, byTime), calls, dueInS, missed, label };
}

/**
 * Checkpoints in a row (newest first) with no gap longer than 2x the interval
 * between them or since the newest one. `times` are checkpoint times in ms.
 */
export function checkpointStreak(times: number[], nowMs: number): number {
  const sorted = [...times].sort((a, b) => b - a);
  if (!sorted.length) return 0;
  const limit = 2 * CHECKPOINT_INTERVAL_S * 1000;
  if (nowMs - sorted[0] > limit) return 0;
  let streak = 1;
  for (let i = 1; i < sorted.length; i += 1) {
    if (sorted[i - 1] - sorted[i] > limit) break;
    streak += 1;
  }
  return streak;
}

// ---------------------------------------------------------------------------
// Lane order: working lanes first, then idle and quiet, settled last; by callsign.
// ---------------------------------------------------------------------------

const STATE_RANK: Record<string, number> = {
  active: 0,
  joining: 1,
  paused: 2,
  idle: 3,
  quiet: 4,
  quota_blocked: 5,
  lost: 6,
  ended: 7,
};

function byLane(a: SessionState, b: SessionState): number {
  return (STATE_RANK[a.state] ?? 9) - (STATE_RANK[b.state] ?? 9) || a.callsign.localeCompare(b.callsign, undefined, { numeric: true });
}

/**
 * Lane order. A sub-agent is its own session (owner decision, gap analysis open question 1):
 * its lane comes right after the lane of the session that started it, nested; a sub-agent
 * whose parent is not shown keeps its own place.
 */
export function laneOrder(sessions: SessionState[]): SessionState[] {
  const ids = new Set(sessions.map((s) => s.id));
  const children = new Map<string, SessionState[]>();
  const roots: SessionState[] = [];
  for (const s of sessions) {
    const parent = s.parent_session_id;
    if (parent && parent !== s.id && ids.has(parent)) {
      const list = children.get(parent) ?? [];
      list.push(s);
      children.set(parent, list);
    } else {
      roots.push(s);
    }
  }
  const out: SessionState[] = [];
  const seen = new Set<string>();
  const visit = (s: SessionState) => {
    if (seen.has(s.id)) return;
    seen.add(s.id);
    out.push(s);
    for (const child of [...(children.get(s.id) ?? [])].sort(byLane)) visit(child);
  };
  for (const s of [...roots].sort(byLane)) visit(s);
  for (const s of sessions) if (!seen.has(s.id)) out.push(s);
  return out;
}

/** How deep a lane is nested: 0 for a session, 1 for its sub-agent, 2 for the sub-agent's own sub-agent. */
export function laneDepth(state: CrewState, session: SessionState): number {
  let depth = 0;
  let parent = session.parent_session_id ?? null;
  const seen = new Set<string>([session.id]);
  while (parent && state.sessions[parent] && !seen.has(parent) && depth < 8) {
    seen.add(parent);
    depth += 1;
    parent = state.sessions[parent].parent_session_id ?? null;
  }
  return depth;
}

export interface SubAgentView {
  /** "sub-agent of cc-1": shown on a sub-agent's lane; the parent answers for its claims and tasks. */
  parentLabel: string | null;
  /** Callsigns of this session's running sub-agents: shown on the parent's lane. */
  subAgents: string[];
}

export function subAgentView(state: CrewState, session: SessionState): SubAgentView {
  const parentId = session.parent_session_id ?? null;
  const parent = parentId ? (state.sessions[parentId] ?? null) : null;
  const parentLabel = parentId ? `sub-agent of ${parent?.callsign ?? 'an ended session'}` : null;
  const subAgents = Object.values(state.sessions)
    .filter((s) => s.parent_session_id === session.id && s.id !== session.id && !['ended', 'lost'].includes(s.state))
    .sort(byLane)
    .map((s) => s.callsign);
  return { parentLabel, subAgents };
}

/** The CLI line another agent runs to pick up a task's baton (D33). */
export function pickupCommand(task: Pick<TaskView, 'number'> | null): string | null {
  return task ? `remembra-crew adopt T-${task.number}` : null;
}

/** The agent page for an agent, optionally focused on one session and one project's crew (§9.10). */
export function agentPageHref(agentId: string, sessionId: string | null = null, project: string | null = null): string {
  return hrefFor('agents', { agent: agentId, session: sessionId, project });
}
