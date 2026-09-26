// The shared crew reducer (spec §4.4), TypeScript implementation.
//
// One reducer spec (docs/crew/reducer.md), two implementations: the Python
// reference (`remembra.crew.reducer`, used by crewd and `remembra-crew watch`)
// and this one. Both must satisfy every vector in tests/crew/vectors/reducer/
// (see __tests__/reducer.vectors.test.ts), so the state below is plain JSON
// and matches the Python state value for value.
//
// Unlike the Python reference, which mutates in place, this reducer never
// mutates its inputs or a previous state: every change copies the path it
// touches (React compares by reference). Unchanged frames return the same
// state object.
//
// Rules in one paragraph: the state starts from a snapshot; events are applied
// strictly by `seq`; a duplicate (`seq <= last_seq`) is ignored; a gap
// (`seq > last_seq + 1`) or a `resync_required` frame sets `needs_resync` and
// every later event is ignored until a new snapshot frame arrives; events of
// another crew are ignored; an event type the reducer does not know (a newer
// server) only advances `last_seq`. Presence frames never change `state`: they
// replace the ephemeral `presence` overlay of a known session.

import {
  LIVE_CLAIM_STATES,
  LIVE_COLLISION_STATES,
  LIVE_DECISION_STATES,
  LIVE_INBOX_STATES,
  type BatonRefEntry,
  type CheckpointView,
  type ClaimView,
  type CollisionView,
  type CrewEvent,
  type CrewSnapshot,
  type CrewState,
  type CrewView,
  type DecisionView,
  type InboxItemView,
  type MessageView,
  type OfferView,
  type PresenceLane,
  type ReportView,
  type SessionState,
  type SessionView,
  type TaskView,
  type ZoneView,
} from './types';

export const MOMENTS_KEEP = 50;
export const MESSAGES_KEEP = 100;
export const BATONS_KEEP = 20;
export const BATON_REFS_KEEP = 50;
const COUNTED_AUDIENCES = ['project', 'crew'] as const;

type Payload = Record<string, unknown>;
type Handler = (state: CrewState, event: EventLike, p: Payload) => CrewState;

/** The envelope fields the reducer reads; any frame from the server satisfies it. */
export interface EventLike {
  seq: number;
  crew_id?: string;
  type: string;
  v?: number;
  ts?: string | null;
  moment?: boolean;
  summary?: string;
  actor?: Partial<CrewEvent['actor']> | null;
  refs?: CrewEvent['refs'] | null;
  payload?: Payload | null;
}

/** Any frame the reducer accepts (unknown frame types are ignored). */
export interface FrameLike {
  type: string;
  crew_id?: string;
  data?: unknown;
  lanes?: PresenceLane[];
  [key: string]: unknown;
}

export function emptyState(): CrewState {
  return {
    crew: null,
    mode: 'solo',
    last_seq: 0,
    needs_resync: false,
    resync_reason: null,
    sessions: {},
    claims: {},
    zones: {},
    tasks: {},
    collisions: {},
    decisions: {},
    offers: {},
    inbox: {},
    inbox_counts: { project: 0, crew: 0 },
    reports: {},
    checkpoints: {},
    hosts: {},
    pending_zone_changes: {},
    messages: [],
    moments: [],
    batons: [],
    baton_refs: {},
    guard_blocks: {},
    tamper_blocks: {},
    budget: {},
  };
}

/** Initial state from `GET /crews/{id}/snapshot` (or a crewd local snapshot). */
export function fromSnapshot(snapshot: CrewSnapshot): CrewState {
  const state = emptyState();
  const crew: CrewView = { ...snapshot.crew };
  state.crew = crew;
  state.mode = crew.mode ?? 'solo';
  state.last_seq = Math.trunc(Number(snapshot.as_of_seq));
  for (const s of snapshot.sessions ?? []) state.sessions[s.id] = { ...s, presence: null };
  for (const c of snapshot.claims ?? []) if (LIVE_CLAIM_STATES.includes(c.state)) state.claims[c.id] = c;
  for (const z of snapshot.zones ?? []) state.zones[z.id] = z;
  for (const t of snapshot.tasks ?? []) state.tasks[t.id] = t;
  for (const c of snapshot.collisions ?? []) if (LIVE_COLLISION_STATES.includes(c.state)) state.collisions[c.id] = c;
  for (const d of snapshot.decisions ?? []) if (LIVE_DECISION_STATES.includes(d.state)) state.decisions[d.id] = d;
  for (const o of snapshot.offers ?? []) state.offers[o.id] = o;
  const counts = snapshot.inbox_counts ?? { project: 0, crew: 0 };
  state.inbox_counts = { project: Math.trunc(Number(counts.project ?? 0)), crew: Math.trunc(Number(counts.crew ?? 0)) };
  for (const changeId of snapshot.pending_zone_changes ?? []) state.pending_zone_changes[changeId] = { loosening: null };
  return state;
}

export function reduce(snapshot: CrewSnapshot, frames: Iterable<FrameLike>): CrewState {
  let state = fromSnapshot(snapshot);
  for (const frame of frames) state = applyFrame(state, frame);
  return state;
}

/** Apply one WebSocket/polling frame. Returns the same state when nothing changed. */
export function applyFrame(state: CrewState, frame: FrameLike): CrewState {
  switch (frame.type) {
    case 'snapshot':
      return fromSnapshot(frame.data as CrewSnapshot);
    case 'crew.event':
      return applyEvent(state, frame.data as EventLike);
    case 'presence':
      return applyPresence(state, frame);
    case 'resync_required':
      if (!crewMatches(state, frame.crew_id)) return state;
      return { ...state, needs_resync: true, resync_reason: 'server' };
    default:
      return state; // crew.subscribed, crew.summary and unknown frames do not change crew state
  }
}

export function applyPresence(state: CrewState, frame: { crew_id?: string; lanes?: PresenceLane[] }): CrewState {
  if (!crewMatches(state, frame.crew_id)) return state;
  let sessions: Record<string, SessionState> | null = null;
  for (const lane of frame.lanes ?? []) {
    const sid = lane.session_id;
    const current = (sessions ?? state.sessions)[sid];
    if (current === undefined) continue;
    const { session_id: _omit, ...overlay } = lane;
    void _omit;
    sessions = sessions ?? { ...state.sessions };
    sessions[sid] = { ...current, presence: overlay };
  }
  return sessions ? { ...state, sessions } : state;
}

export function applyEvent(state: CrewState, event: EventLike): CrewState {
  if (!crewMatches(state, event.crew_id)) return state;
  if (state.needs_resync) return state;
  const seq = Math.trunc(Number(event.seq));
  if (seq <= state.last_seq) return state; // duplicate or replay overlap
  if (seq > state.last_seq + 1) return { ...state, needs_resync: true, resync_reason: 'gap' };
  const handler = Object.prototype.hasOwnProperty.call(HANDLERS, event.type) ? HANDLERS[event.type] : undefined;
  let next = state;
  if (handler !== undefined && Math.trunc(Number(event.v ?? 1)) === 1) {
    next = handler(state, event, (event.payload || {}) as Payload);
  }
  next = { ...next, last_seq: seq, crew: next.crew !== null ? { ...next.crew, last_seq: seq } : null };
  if (event.moment) {
    const moments = [...next.moments, { seq, type: event.type, summary: event.summary ?? '', ts: event.ts ?? null }];
    next.moments = moments.slice(-MOMENTS_KEEP);
  }
  return next;
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

function crewMatches(state: CrewState, crewId: unknown): boolean {
  return state.crew === null || state.crew.id === crewId;
}

/** The session an event concerns: `refs.session_id`, else the actor when it is a session. */
export function eventSessionId(event: EventLike): string | null {
  const refs = event.refs ?? {};
  if (refs.session_id) return String(refs.session_id);
  const actor = event.actor ?? {};
  return actor.kind === 'session' && actor.id ? String(actor.id) : null;
}

function withSession(state: CrewState, event: EventLike, change: (s: SessionState) => SessionState): CrewState {
  const sid = eventSessionId(event);
  const current = sid ? state.sessions[sid] : undefined;
  if (!sid || current === undefined) return state;
  return { ...state, sessions: { ...state.sessions, [sid]: change(current) } };
}

function put<T>(map: Record<string, T>, key: string, value: T): Record<string, T> {
  return { ...map, [key]: value };
}

function drop<T>(map: Record<string, T>, key: string): Record<string, T> {
  if (!Object.prototype.hasOwnProperty.call(map, key)) return map;
  const next = { ...map };
  delete next[key];
  return next;
}

const crewCreated: Handler = (state, _e, p) => {
  const crew = { ...(p.crew as CrewView) };
  return { ...state, crew, mode: crew.mode };
};

const crewSettings: Handler = (state, _e, p) => {
  if (state.crew === null) return state;
  const crew: CrewView = { ...state.crew, settings_version: p.settings_version as number };
  if (p.enforcement) crew.enforcement = p.enforcement as CrewView['enforcement'];
  return { ...state, crew };
};

const crewMode: Handler = (state, _e, p) => {
  const to = p.to as CrewView['mode'];
  return { ...state, mode: to, crew: state.crew !== null ? { ...state.crew, mode: to } : null };
};

const host: Handler = (state, event, p) => {
  if (event.type === 'host.registered') {
    const h = p.host as { id: string; state: CrewState['hosts'][string]['state'] };
    return { ...state, hosts: put(state.hosts, h.id, { id: h.id, state: h.state }) };
  }
  const hostId = p.host_id as string;
  const next = event.type === 'host.unreachable' ? 'unreachable' : 'online';
  return { ...state, hosts: put(state.hosts, hostId, { id: hostId, state: next }) };
};

const sessionJoined: Handler = (state, _e, p) => {
  const s = p.session as SessionView;
  return { ...state, sessions: put(state.sessions, s.id, { ...s, presence: null }) };
};

const sessionChange: Handler = (state, event, p) =>
  withSession(state, event, (s) => {
    switch (event.type) {
      case 'session.state_changed': {
        const to = p.to as SessionState['state'];
        return {
          ...s,
          state: to,
          quiet_reason: to === 'quiet' ? ((p.quiet_reason ?? null) as SessionState['quiet_reason']) : null,
          state_reason: (p.reason ?? null) as string | null,
        };
      }
      case 'session.recovered':
        return { ...s, state: 'active', quiet_reason: null, state_reason: null };
      case 'session.quota_blocked':
        // a StopFailure recorded after SessionEnd (S0: the order is unstable) never revives an ended lane
        return { ...s, state: s.state !== 'ended' ? 'quota_blocked' : s.state, state_reason: (p.error ?? null) as string | null };
      case 'session.limit_warning':
        return {
          ...s,
          limit: {
            level: p.level as NonNullable<SessionState['limit']>['level'],
            pct: (p.pct ?? null) as number | null,
            source: p.source as NonNullable<SessionState['limit']>['source'],
          },
        };
      case 'session.stuck':
        return { ...s, stuck: Boolean(p.stuck) };
      case 'session.paused':
        return { ...s, state: 'paused' };
      case 'session.resumed':
        return { ...s, state: p.to as SessionState['state'] };
      case 'session.left':
        return { ...s, state: 'ended', end_reason: (p.reason ?? null) as string | null, ended_at: event.ts ?? null, presence: null };
      case 'session.lost':
        return { ...s, state: 'lost', state_reason: (p.reason ?? null) as string | null, presence: null };
      default:
        return s;
    }
  });

const activity: Handler = (state, event, p) =>
  withSession(state, event, (s) => {
    const next: SessionState = { ...s, last_activity_at: event.ts ?? null };
    if (event.type === 'activity.commit') next.head_commit = p.sha as string;
    return next;
  });

const zone: Handler = (state, event, p) => {
  const z = p.zone as ZoneView;
  if (event.type === 'zone.archived') return { ...state, zones: drop(state.zones, z.id) };
  return { ...state, zones: put(state.zones, z.id, z) };
};

const zoneChange: Handler = (state, event, p) => {
  const changeId = p.change_id as string;
  if (event.type === 'zone.change_pending') {
    return { ...state, pending_zone_changes: put(state.pending_zone_changes, changeId, { loosening: Boolean(p.loosening) }) };
  }
  return { ...state, pending_zone_changes: drop(state.pending_zone_changes, changeId) };
};

function isEmptyObject(value: unknown): boolean {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).length === 0;
}

const claim: Handler = (state, _e, p) => {
  const c = p.claim as ClaimView | null | undefined;
  if (!c || isEmptyObject(c)) return state;
  const claims = LIVE_CLAIM_STATES.includes(c.state) ? put(state.claims, c.id, c) : drop(state.claims, c.id);
  let offers = state.offers;
  if (c.state !== 'reserved') {
    const stale = Object.entries(offers).filter(([, o]) => o.claim_id === c.id);
    if (stale.length) {
      offers = { ...offers };
      for (const [oid] of stale) delete offers[oid];
    }
  }
  return { ...state, claims, offers };
};

const claimOffered: Handler = (state, _e, p) => {
  const offer = p.offer as OfferView;
  return { ...state, offers: put(state.offers, offer.id, offer) };
};

const claimFenced: Handler = (state, _e, p) => {
  const id = p.claim_id as string;
  const current = state.claims[id];
  if (current === undefined) return state;
  return { ...state, claims: put(state.claims, id, { ...current, fenced: true }) };
};

const batonPassed: Handler = (state, event, p) => {
  const entry = { seq: event.seq, ts: event.ts ?? null, ...p };
  return { ...state, batons: [...state.batons, entry].slice(-BATONS_KEEP) };
};

const batonRef: Handler = (state, event, p) => {
  const refs: Record<string, BatonRefEntry> = { ...state.baton_refs };
  refs[p.ref as string] = {
    seq: event.seq,
    task_id: (p.task_id ?? null) as string | null,
    session_id: eventSessionId(event),
    dirty_files: p.dirty_files as number,
    unpushed: p.unpushed as number,
  };
  let keys = Object.keys(refs);
  while (keys.length > BATON_REFS_KEEP) {
    let oldest = keys[0];
    for (const key of keys) if (refs[key].seq < refs[oldest].seq) oldest = key;
    delete refs[oldest];
    keys = Object.keys(refs);
  }
  return { ...state, baton_refs: refs };
};

const guard: Handler = (state, event, p) => {
  const sid = eventSessionId(event);
  if (sid === null) return state;
  if (event.type === 'guard.blocked') {
    const add = Math.trunc(Number(p.coalesced ?? 1));
    return { ...state, guard_blocks: put(state.guard_blocks, sid, (state.guard_blocks[sid] ?? 0) + add) };
  }
  return { ...state, tamper_blocks: put(state.tamper_blocks, sid, (state.tamper_blocks[sid] ?? 0) + 1) };
};

const githook: Handler = (state, event, p) =>
  withSession(state, event, (s) => ({ ...s, githook_state: p.state as SessionState['githook_state'] }));

const collision: Handler = (state, _e, p) => {
  const c = p.collision as CollisionView;
  const collisions = LIVE_COLLISION_STATES.includes(c.state) ? put(state.collisions, c.id, c) : drop(state.collisions, c.id);
  return { ...state, collisions };
};

const task: Handler = (state, _e, p) => {
  const t = p.task as TaskView;
  return { ...state, tasks: put(state.tasks, t.id, t) };
};

const checkpoint: Handler = (state, _e, p) => {
  const c = p.checkpoint as CheckpointView;
  return { ...state, checkpoints: put(state.checkpoints, c.session_id, c) };
};

const report: Handler = (state, _e, p) => {
  const r = p.report as ReportView;
  const current = state.reports[r.task_id];
  if (r.is_current) return { ...state, reports: put(state.reports, r.task_id, r) };
  if (current !== undefined && current.id === r.id) return { ...state, reports: drop(state.reports, r.task_id) };
  return state;
};

const message: Handler = (state, event, p) => {
  if (event.type === 'message.posted') {
    const msgs = [...state.messages, p.message as MessageView];
    msgs.sort((a, b) => a.seq - b.seq);
    return { ...state, messages: msgs.slice(-MESSAGES_KEEP) };
  }
  const target = event.type === 'message.edited' ? (p.message as MessageView).id : (p.message_id as string);
  const idx = state.messages.findIndex((m) => m.id === target);
  if (idx < 0) return state;
  const msgs = [...state.messages];
  msgs[idx] =
    event.type === 'message.edited'
      ? (p.message as MessageView)
      : { ...msgs[idx], body: '', body_truncated: false, redacted: true };
  return { ...state, messages: msgs };
};

const decision: Handler = (state, _e, p) => {
  const d = p.decision as DecisionView;
  const decisions = LIVE_DECISION_STATES.includes(d.state) ? put(state.decisions, d.id, d) : drop(state.decisions, d.id);
  return { ...state, decisions };
};

const inbox: Handler = (state, event, p) => {
  const item = p.item as InboxItemView;
  const audience = item.audience;
  const counted = (COUNTED_AUDIENCES as readonly string[]).includes(audience);
  const known = Object.prototype.hasOwnProperty.call(state.inbox, item.id);
  const counts = { ...state.inbox_counts };
  if (LIVE_INBOX_STATES.includes(item.state)) {
    if (!known && event.type === 'inbox.item_created' && counted) counts[audience as 'project' | 'crew'] += 1;
    return { ...state, inbox: put(state.inbox, item.id, item), inbox_counts: counts };
  }
  // resolved or dismissed: it was counted either here (known) or in the snapshot (not known)
  if (counted) {
    const key = audience as 'project' | 'crew';
    counts[key] = Math.max(0, counts[key] - 1);
  }
  return { ...state, inbox: drop(state.inbox, item.id), inbox_counts: counts };
};

const budget: Handler = (state, event, p) => ({
  ...state,
  budget: put(state.budget, p.metric as string, {
    used: p.used as number,
    limit: p.limit as number,
    capped: event.type === 'budget.cap_reached',
  }),
});

const noop: Handler = (state) => state;

const HANDLERS: Record<string, Handler> = {
  'crew.created': crewCreated,
  'crew.settings_changed': crewSettings,
  'crew.mode_changed': crewMode,
  'crew.shift_started': noop,
  'crew.shift_ended': noop,
  'host.registered': host,
  'host.unreachable': host,
  'host.recovered': host,
  'session.joined': sessionJoined,
  'session.state_changed': sessionChange,
  'session.recovered': sessionChange,
  'session.quota_blocked': sessionChange,
  'session.limit_warning': sessionChange,
  'session.stuck': sessionChange,
  'session.paused': sessionChange,
  'session.resumed': sessionChange,
  'session.left': sessionChange,
  'session.lost': sessionChange,
  'session.token_rotated': noop,
  'activity.burst': activity,
  'activity.commit': activity,
  'activity.push': activity,
  'activity.deploy': activity,
  'activity.test_verdict_changed': activity,
  'zone.created': zone,
  'zone.updated': zone,
  'zone.archived': zone,
  'zone.frozen': zone,
  'zone.unfrozen': zone,
  'zone.synced': noop,
  'zone.change_pending': zoneChange,
  'zone.change_decided': zoneChange,
  'zone.suggested_applied': noop,
  'claim.requested': claim,
  'claim.granted': claim,
  'claim.queued': claim,
  'claim.denied': claim,
  'claim.released': claim,
  'claim.expired': claim,
  'claim.reserved': claim,
  'claim.adopted': claim,
  'claim.offered_in_brief': claimOffered,
  'claim.handover_offered': claim,
  'claim.handover_accepted': claim,
  'claim.handover_declined': claim,
  'claim.revoked': claim,
  'claim.transferred': claim,
  'claim.fenced': claimFenced,
  'claim.unconfirmed': claim,
  'baton.passed': batonPassed,
  'baton.ref_created': batonRef,
  'guard.blocked': guard,
  'guard.bypass_used': noop,
  'guard.tamper_blocked': guard,
  'gate.error': noop,
  'gate.deadline': noop,
  'gate.tampered': noop,
  'githook.missing': githook,
  'collision.detected': collision,
  'collision.acknowledged': collision,
  'collision.resolved': collision,
  'collision.dismissed': collision,
  'collision.escalated': collision,
  'task.created': task,
  'task.updated': task,
  'task.status_changed': task,
  'task.assigned': task,
  'task.stalled': task,
  'task.recovered': task,
  'task.review_requested': task,
  'task.review_decided': task,
  'task.done': task,
  'task.reopened': task,
  'task.deps_changed': task,
  'task.acceptance_changed': task,
  'checkpoint.created': checkpoint,
  'checkpoint.missed': noop,
  'report.submitted': report,
  'report.accepted': report,
  'report.rejected': report,
  'report.waived': report,
  'report.superseded': report,
  'handoff.created': noop,
  'message.posted': message,
  'message.edited': message,
  'message.redacted': message,
  'decision.proposed': decision,
  'decision.confirmed': decision,
  'decision.rejected': decision,
  'decision.superseded': decision,
  'inbox.item_created': inbox,
  'inbox.item_claimed': inbox,
  'inbox.item_resolved': inbox,
  'human.override': noop,
  'budget.warning': budget,
  'budget.cap_reached': budget,
};

/** Every event type this reducer handles (the L0 closed set, §4.2). */
export const HANDLED_EVENT_TYPES: readonly string[] = Object.freeze(Object.keys(HANDLERS));
