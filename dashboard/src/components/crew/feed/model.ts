// Event Feed model (spec §9.6): pure functions from the crew event log to the
// rows the feed shows. Newest first. Special rows for batons, collapsed
// checkpoint runs, completions, collisions, decisions, guard blocks, bypass
// codes and tamper blocks. Every row carries a glyph *and* a word, so status
// never depends on colour alone (§9). Free text from agents (headlines,
// titles, message bodies) is returned as plain strings for the row to render
// as text, with the author's key-verified / self-declared label.

import type { FeedFilters } from '../../../lib/crew/routes';
import type {
  CheckpointView,
  CollisionView,
  CrewEvent,
  CrewState,
  DecisionView,
  MessageView,
  ReportView,
  Severity,
  TaskView,
} from '../../../lib/crew/types';

export type RowKind = 'baton' | 'checkpoints' | 'completion' | 'collision' | 'decision' | 'guard' | 'bypass' | 'tamper' | 'event';

export type RowTone = 'neutral' | 'signal' | 'ok' | 'fail' | 'warn';

export interface FeedRow {
  /** Stable across new arrivals (a checkpoint run is keyed by its oldest event). */
  key: string;
  kind: RowKind;
  /** The newest event of the row. */
  event: CrewEvent;
  /** Every event in a collapsed checkpoint run, newest first (length 1 otherwise). */
  events: CrewEvent[];
  /** True for a run of ≥2 checkpoints shown as one row. */
  collapsed: boolean;
}

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

export function rowKind(type: string): RowKind | 'checkpoint' {
  switch (type) {
    case 'baton.passed':
      return 'baton';
    case 'checkpoint.created':
      return 'checkpoint';
    case 'task.done':
    case 'report.accepted':
    case 'report.waived':
      return 'completion';
    case 'collision.detected':
    case 'collision.escalated':
      return 'collision';
    case 'decision.proposed':
    case 'decision.confirmed':
    case 'decision.rejected':
    case 'decision.superseded':
      return 'decision';
    case 'guard.blocked':
      return 'guard';
    case 'guard.bypass_used':
      return 'bypass';
    case 'guard.tamper_blocked':
    case 'gate.tampered':
      return 'tamper';
    default:
      return 'event';
  }
}

function obj(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

export function payloadCheckpoint(e: CrewEvent): CheckpointView | null {
  return (obj(e.payload.checkpoint) as CheckpointView | null) ?? null;
}
export function payloadCollision(e: CrewEvent): CollisionView | null {
  return (obj(e.payload.collision) as CollisionView | null) ?? null;
}
export function payloadDecision(e: CrewEvent): DecisionView | null {
  return (obj(e.payload.decision) as DecisionView | null) ?? null;
}
export function payloadTask(e: CrewEvent): TaskView | null {
  return (obj(e.payload.task) as TaskView | null) ?? null;
}
export function payloadReport(e: CrewEvent): ReportView | null {
  return (obj(e.payload.report) as ReportView | null) ?? null;
}
export function payloadMessage(e: CrewEvent): MessageView | null {
  return (obj(e.payload.message) as MessageView | null) ?? null;
}

/** The session an event is about: refs first, then the acting session. */
export function eventSessionId(e: CrewEvent): string | null {
  return e.refs.session_id ?? (e.actor.kind === 'session' ? e.actor.id : null);
}

/** The zone ids an event touches (refs, then payload objects). */
export function eventZoneIds(e: CrewEvent): string[] {
  const out = new Set<string>();
  if (e.refs.zone_id) out.add(e.refs.zone_id);
  const p = e.payload;
  const claim = obj(p.claim);
  if (claim && str(claim.zone_id)) out.add(claim.zone_id as string);
  const zone = obj(p.zone);
  if (zone && str(zone.id)) out.add(zone.id as string);
  const col = payloadCollision(e);
  if (col?.zone_id) out.add(col.zone_id);
  const dec = payloadDecision(e);
  if (dec?.zone_id) out.add(dec.zone_id);
  const task = payloadTask(e);
  for (const z of task?.zone_ids ?? []) out.add(z);
  if (Array.isArray(p.zones)) for (const z of p.zones) if (str(z)) out.add(z as string);
  if (Array.isArray(p.zone_ids)) for (const z of p.zone_ids) if (str(z)) out.add(z as string);
  return [...out];
}

/** Zone slugs named directly in a payload (guard.blocked carries the slug, not the id). */
export function eventZoneSlugs(e: CrewEvent): string[] {
  const out: string[] = [];
  const zone = str(e.payload.zone);
  if (zone) out.push(zone);
  const z = obj(e.payload.zone);
  if (z && str(z.slug)) out.push(z.slug as string);
  return out;
}

/** The task id an event is about (refs, then payload). */
export function eventTaskId(e: CrewEvent): string | null {
  if (e.refs.task_id) return e.refs.task_id;
  const p = e.payload;
  const task = payloadTask(e);
  if (task?.id) return task.id;
  if (str(p.task_id)) return p.task_id as string;
  const claim = obj(p.claim);
  if (claim && str(claim.task_id)) return claim.task_id as string;
  const cp = payloadCheckpoint(e);
  if (cp?.task_id) return cp.task_id;
  const rp = payloadReport(e);
  if (rp?.task_id) return rp.task_id;
  const dec = payloadDecision(e);
  if (dec?.task_id) return dec.task_id;
  return null;
}

/** Every session id an event involves (actor, refs, baton ends, collision parties). */
export function eventSessionIds(e: CrewEvent): string[] {
  const out = new Set<string>();
  const own = eventSessionId(e);
  if (own) out.add(own);
  const p = e.payload;
  for (const k of ['from_session', 'to_session']) if (str(p[k])) out.add(p[k] as string);
  const col = payloadCollision(e);
  if (col?.session_a) out.add(col.session_a);
  if (col?.session_b) out.add(col.session_b);
  const claim = obj(p.claim);
  if (claim && str(claim.holder_session_id)) out.add(claim.holder_session_id as string);
  return [...out];
}

// ---------------------------------------------------------------------------
// Filters (URL, §9.1: type, session, zone, task, moments)
// ---------------------------------------------------------------------------

export interface TypeGroup {
  id: string;
  label: string;
  prefixes: string[];
}

/** Filter chips. The URL keeps the prefixes, so a chip is "on" when all its prefixes are. */
export const TYPE_GROUPS: TypeGroup[] = [
  { id: 'batons', label: 'Batons', prefixes: ['baton.', 'handoff.'] },
  { id: 'claims', label: 'Claims & zones', prefixes: ['claim.', 'zone.'] },
  { id: 'tasks', label: 'Tasks & reports', prefixes: ['task.', 'report.', 'checkpoint.'] },
  { id: 'guard', label: 'Guard & collisions', prefixes: ['guard.', 'gate.', 'githook.', 'collision.'] },
  { id: 'channel', label: 'Channel', prefixes: ['message.', 'decision.', 'inbox.', 'human.'] },
  { id: 'sessions', label: 'Sessions', prefixes: ['session.', 'host.', 'crew.', 'budget.'] },
  { id: 'work', label: 'Commits & tests', prefixes: ['activity.'] },
];

export function groupActive(filters: FeedFilters, group: TypeGroup): boolean {
  return group.prefixes.every((p) => filters.types.includes(p));
}

/** Toggle a chip: add or remove all of its prefixes. */
export function toggleGroup(filters: FeedFilters, group: TypeGroup): FeedFilters {
  const on = groupActive(filters, group);
  const types = on ? filters.types.filter((t) => !group.prefixes.includes(t)) : [...new Set([...filters.types, ...group.prefixes])];
  return { ...filters, types };
}

export function hasFilters(filters: FeedFilters): boolean {
  return filters.types.length > 0 || !!filters.session || !!filters.zone || !!filters.task || filters.moments;
}

export const NO_FILTERS: FeedFilters = { types: [], session: null, zone: null, task: null, moments: false };

function typeMatches(type: string, patterns: string[]): boolean {
  return patterns.some((p) => (p.endsWith('.') ? type.startsWith(p) : type === p || type.startsWith(`${p}.`)));
}

/** Resolve a zone filter value (slug or id) to the zone's id and slug. */
function zoneKeys(state: CrewState | null, value: string): { id: string | null; slug: string } {
  const zones = state ? Object.values(state.zones) : [];
  const byId = zones.find((z) => z.id === value);
  if (byId) return { id: byId.id, slug: byId.slug };
  const bySlug = zones.find((z) => z.slug === value);
  return { id: bySlug?.id ?? null, slug: bySlug?.slug ?? value };
}

/** Resolve a task filter value (`T-14`, `14` or an id) to the task id. */
export function taskIdFor(state: CrewState | null, value: string): string {
  const m = /^T-?(\d+)$/i.exec(value.trim()) ?? /^(\d+)$/.exec(value.trim());
  if (m && state) {
    const n = Number(m[1]);
    const task = Object.values(state.tasks).find((t) => t.number === n);
    if (task) return task.id;
  }
  return value;
}

/** Resolve a session filter value (callsign or id) to the session id. */
export function sessionIdFor(state: CrewState | null, value: string): string {
  if (!state) return value;
  if (state.sessions[value]) return value;
  const s = Object.values(state.sessions).find((x) => x.callsign === value);
  return s?.id ?? value;
}

export function matchesFilters(e: CrewEvent, filters: FeedFilters, state: CrewState | null): boolean {
  if (filters.moments && !e.moment) return false;
  if (filters.types.length && !typeMatches(e.type, filters.types)) return false;
  if (filters.session) {
    const sid = sessionIdFor(state, filters.session);
    if (!eventSessionIds(e).includes(sid)) return false;
  }
  if (filters.zone) {
    const { id, slug } = zoneKeys(state, filters.zone);
    const ids = eventZoneIds(e);
    const slugs = eventZoneSlugs(e);
    if (!(id && ids.includes(id)) && !slugs.includes(slug) && !ids.includes(filters.zone)) return false;
  }
  if (filters.task) {
    const tid = taskIdFor(state, filters.task);
    if (eventTaskId(e) !== tid) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Rows (newest first, checkpoint runs collapsed)
// ---------------------------------------------------------------------------

/**
 * Build the feed rows from events in any order. Consecutive checkpoints by the
 * same session (with nothing else in between, after filtering) collapse into
 * one row unless its key is in `expanded`.
 */
export function buildRows(events: readonly CrewEvent[], filters: FeedFilters, state: CrewState | null, expanded: ReadonlySet<string> = new Set()): FeedRow[] {
  const visible = events.filter((e) => matchesFilters(e, filters, state)).sort((a, b) => b.seq - a.seq);
  const rows: FeedRow[] = [];
  let i = 0;
  while (i < visible.length) {
    const e = visible[i];
    const kind = rowKind(e.type);
    if (kind === 'checkpoint') {
      const sid = eventSessionId(e);
      let j = i + 1;
      while (j < visible.length && rowKind(visible[j].type) === 'checkpoint' && eventSessionId(visible[j]) === sid) j += 1;
      const run = visible.slice(i, j);
      const key = `cp:${run[run.length - 1].seq}`;
      if (run.length > 1 && !expanded.has(key)) {
        rows.push({ key, kind: 'checkpoints', event: run[0], events: run, collapsed: true });
      } else {
        // an expanded run: one row per checkpoint (runKeyOf finds the run again to collapse it)
        for (const ev of run) rows.push({ key: `e:${ev.seq}`, kind: 'checkpoints', event: ev, events: [ev], collapsed: false });
      }
      i = j;
      continue;
    }
    rows.push({ key: `e:${e.seq}`, kind, event: e, events: [e], collapsed: false });
    i += 1;
  }
  return rows;
}

/** The run key of an expanded checkpoint row (so the row can offer "collapse"), or null. */
export function runKeyOf(rows: readonly FeedRow[], index: number): string | null {
  const row = rows[index];
  if (!row || row.kind !== 'checkpoints' || row.collapsed) return null;
  const sid = eventSessionId(row.event);
  let end = index;
  while (end + 1 < rows.length && rows[end + 1].kind === 'checkpoints' && !rows[end + 1].collapsed && eventSessionId(rows[end + 1].event) === sid) end += 1;
  let start = index;
  while (start - 1 >= 0 && rows[start - 1].kind === 'checkpoints' && !rows[start - 1].collapsed && eventSessionId(rows[start - 1].event) === sid) start -= 1;
  if (end === start) return null;
  return `cp:${rows[end].event.seq}`;
}

// ---------------------------------------------------------------------------
// Presentation
// ---------------------------------------------------------------------------

export interface RowLook {
  glyph: string;
  /** The word that carries the status (never colour alone). */
  label: string;
  tone: RowTone;
}

const SEVERITY_RANK: Record<Severity, number> = { info: 0, notice: 1, low: 2, medium: 3, high: 4, critical: 5 };

export function collisionSeverity(e: CrewEvent): Severity {
  const s = payloadCollision(e)?.severity ?? e.severity;
  return s in SEVERITY_RANK ? s : 'info';
}

/** Glyphs follow the lane activity strip (§9.3): ◆ checkpoint ▲ guard block ✕ failing test ★ done ⇢ baton ⬆ push ⛔ tamper. */
export function rowLook(row: Pick<FeedRow, 'kind' | 'event' | 'collapsed' | 'events'>): RowLook {
  const e = row.event;
  switch (row.kind) {
    case 'baton':
      return { glyph: '⇢', label: 'baton pass', tone: 'signal' };
    case 'checkpoints':
      return { glyph: '◆', label: row.collapsed ? `${row.events.length} checkpoints` : 'checkpoint', tone: 'neutral' };
    case 'completion':
      return e.type === 'report.waived'
        ? { glyph: '★', label: 'waived by a human', tone: 'ok' }
        : { glyph: '★', label: e.type === 'task.done' ? 'done' : 'report accepted', tone: 'ok' };
    case 'collision': {
      const sev = collisionSeverity(e);
      const high = SEVERITY_RANK[sev] >= SEVERITY_RANK.high;
      return { glyph: '⚠', label: `collision · ${sev}`, tone: high ? 'fail' : 'warn' };
    }
    case 'decision': {
      const d = payloadDecision(e);
      if (e.type === 'decision.proposed') return { glyph: '§', label: 'decision to confirm', tone: 'signal' };
      if (e.type === 'decision.confirmed') return { glyph: '§', label: 'decision in force', tone: 'ok' };
      return { glyph: '§', label: `decision ${d?.state === 'superseded' || e.type === 'decision.superseded' ? 'superseded' : 'rejected'}`, tone: 'neutral' };
    }
    case 'guard':
      return { glyph: '▲', label: str(e.payload.decision) === 'would_deny' ? 'would block (observe)' : 'blocked', tone: 'warn' };
    case 'bypass':
      return { glyph: '⚑', label: 'bypass code used', tone: 'fail' };
    case 'tamper':
      return { glyph: '⛔︎', label: e.type === 'gate.tampered' ? 'gate tampered' : 'tamper blocked', tone: 'fail' };
    default:
      return plainLook(e);
  }
}

function plainLook(e: CrewEvent): RowLook {
  const t = e.type;
  if (t === 'activity.push') return { glyph: '⬆', label: 'push', tone: 'neutral' };
  if (t === 'activity.commit') return { glyph: '●', label: 'commit', tone: 'neutral' };
  if (t === 'activity.deploy') return { glyph: '⬆', label: `deploy ${str(e.payload.status) ?? ''}`.trim(), tone: 'neutral' };
  if (t === 'activity.test_verdict_changed') {
    const failing = str(e.payload.to) === 'fail';
    return failing ? { glyph: '✕', label: 'tests failing', tone: 'fail' } : { glyph: '✓', label: 'tests passing', tone: 'ok' };
  }
  if (t === 'activity.burst') return { glyph: '·', label: 'working', tone: 'neutral' };
  if (t === 'session.quota_blocked') return { glyph: '⇣', label: 'stopped: limit', tone: 'signal' };
  if (t === 'session.lost') return { glyph: '⇣', label: 'lost', tone: 'signal' };
  if (t === 'session.recovered') return { glyph: '↺', label: 'back', tone: 'ok' };
  if (t === 'session.joined') return { glyph: '→', label: 'joined', tone: 'neutral' };
  if (t === 'session.left') return { glyph: '←', label: 'left', tone: 'neutral' };
  if (t === 'session.stuck') return { glyph: '⚠', label: 'stuck', tone: 'warn' };
  if (t === 'session.limit_warning') return { glyph: '◔', label: 'limit warning', tone: 'warn' };
  if (t === 'session.paused') return { glyph: '⏸︎', label: 'paused', tone: 'neutral' };
  if (t === 'host.unreachable') return { glyph: '⇣', label: 'host offline', tone: 'warn' };
  if (t === 'crew.mode_changed' && str(e.payload.to) === 'multi') return { glyph: '✦', label: 'crew assembled', tone: 'neutral' };
  if (t === 'claim.reserved') return { glyph: '✦', label: 'baton held', tone: 'signal' };
  if (t === 'claim.adopted') return { glyph: '⇠', label: 'baton taken', tone: 'signal' };
  if (t === 'claim.denied') return { glyph: '▲', label: 'claim denied', tone: 'warn' };
  if (t.startsWith('claim.')) return { glyph: '▨', label: t.slice(6).replace(/_/g, ' '), tone: 'neutral' };
  if (t === 'zone.change_pending') return { glyph: '⚑', label: 'zone change to approve', tone: 'signal' };
  if (t === 'zone.frozen') return { glyph: '❄︎', label: 'frozen', tone: 'neutral' };
  if (t.startsWith('zone.')) return { glyph: '▦', label: `zone ${t.slice(5).replace(/_/g, ' ')}`, tone: 'neutral' };
  if (t === 'report.rejected') return { glyph: '✕', label: 'report rejected', tone: 'fail' };
  if (t === 'task.stalled') return { glyph: '⇣', label: 'task stalled', tone: 'signal' };
  if (t.startsWith('task.') || t.startsWith('report.')) return { glyph: '▢', label: t.replace('.', ' ').replace(/_/g, ' '), tone: 'neutral' };
  if (t === 'checkpoint.missed') return { glyph: '◇', label: 'checkpoint missed', tone: 'warn' };
  if (t === 'githook.missing') return { glyph: '⚠', label: 'git hook missing', tone: 'fail' };
  if (t === 'gate.error' || t === 'gate.deadline') return { glyph: '⚠', label: t === 'gate.error' ? 'gate error' : 'gate slow', tone: 'warn' };
  if (t.startsWith('message.')) return { glyph: '›', label: t === 'message.posted' ? 'message' : t.slice(8), tone: 'neutral' };
  if (t === 'human.override') return { glyph: '✋︎', label: 'human override', tone: 'neutral' };
  if (t.startsWith('inbox.')) return { glyph: '▤', label: t.slice(6).replace(/_/g, ' '), tone: 'neutral' };
  if (t.startsWith('budget.')) return { glyph: '◔', label: t === 'budget.cap_reached' ? 'cap reached' : 'budget warning', tone: 'warn' };
  if (t === 'handoff.created') return { glyph: '⇢', label: 'handoff written', tone: 'neutral' };
  return { glyph: '·', label: t.replace(/_/g, ' '), tone: 'neutral' };
}

export interface ActorText {
  name: string;
  /** "key-verified", "self-declared", "you" (a dashboard login) or "server". */
  trust: string;
}

export function actorText(e: CrewEvent): ActorText {
  const a = e.actor;
  if (a.kind === 'human') return { name: 'you', trust: 'human' };
  if (a.kind === 'system') return { name: 'server', trust: 'server' };
  return { name: a.callsign ?? a.id, trust: a.verified ? 'key-verified' : 'self-declared' };
}

export function callsign(state: CrewState | null, sessionId: string | null | undefined): string | null {
  if (!sessionId) return null;
  return state?.sessions[sessionId]?.callsign ?? sessionId;
}

export function zoneSlug(state: CrewState | null, zoneId: string): string {
  return state?.zones[zoneId]?.slug ?? zoneId;
}

export function taskLabel(state: CrewState | null, taskId: string | null, e?: CrewEvent): string | null {
  if (!taskId) return null;
  const known = state?.tasks[taskId];
  if (known) return `T-${known.number}`;
  const fromPayload = e ? payloadTask(e) : null;
  if (fromPayload?.id === taskId && typeof fromPayload.number === 'number') return `T-${fromPayload.number}`;
  return taskId;
}

const DETAIL_MAX = 180;

function clip(text: string): string {
  const one = text.replace(/\s+/g, ' ').trim();
  return one.length > DETAIL_MAX ? `${one.slice(0, DETAIL_MAX - 1)}…` : one;
}

/**
 * Free text that belongs to the event (untrusted, rendered as plain text):
 * a checkpoint headline, a task or decision title, a message body.
 */
export function detailText(e: CrewEvent): string | null {
  const cp = payloadCheckpoint(e);
  if (cp?.headline) return clip(cp.headline);
  const dec = payloadDecision(e);
  if (dec) return clip(dec.title && dec.decision ? `${dec.title}: ${dec.decision}` : dec.title || dec.decision);
  const msg = payloadMessage(e);
  if (msg) return msg.redacted ? '(redacted)' : clip(msg.body);
  const task = payloadTask(e);
  if (task?.title) return clip(task.title);
  const col = payloadCollision(e);
  if (col?.subject) return clip(`${col.kind.replace(/_/g, ' ')} on ${col.subject}`);
  if (e.type === 'guard.blocked') {
    const path = str(e.payload.path_rel);
    const holder = str(e.payload.holder);
    const zone = str(e.payload.zone);
    const parts = [path, zone ? `zone ${zone}` : null, holder ? `held by ${holder}` : null].filter(Boolean);
    return parts.length ? parts.join(' · ') : null;
  }
  if (e.type === 'zone.change_pending') return str(e.payload.diff_summary);
  return null;
}

/** Whether the detail text was written by an agent (and needs the trust label next to it). */
export function detailFromAgent(e: CrewEvent): boolean {
  if (e.type === 'guard.blocked' || e.type === 'zone.change_pending') return false;
  const msg = payloadMessage(e);
  if (msg) return msg.author_kind === 'agent';
  const dec = payloadDecision(e);
  if (dec) return dec.decided_by_kind === 'agent';
  return e.actor.kind === 'session';
}

export interface BatonText {
  from: string | null;
  to: string;
  zones: string[];
  task: string | null;
  restored: boolean | null;
  savedWork: boolean;
  kind: string;
}

export function batonText(e: CrewEvent, state: CrewState | null): BatonText {
  const p = e.payload;
  const zones = Array.isArray(p.zones) ? (p.zones as unknown[]).filter((z): z is string => typeof z === 'string').map((z) => zoneSlug(state, z)) : [];
  return {
    from: callsign(state, str(p.from_session)),
    to: callsign(state, str(p.to_session)) ?? e.actor.callsign ?? 'next agent',
    zones,
    task: taskLabel(state, str(p.task_id), e),
    restored: typeof p.restored === 'boolean' ? p.restored : null,
    savedWork: !!str(p.baton_ref),
    kind: (str(p.kind) ?? 'handover').replace(/_/g, ' '),
  };
}

/** Short reference chips for a row: the task (T-14) and up to two zone slugs. */
export function refChips(row: FeedRow, state: CrewState | null): string[] {
  const e = row.event;
  const chips: string[] = [];
  const task = taskLabel(state, eventTaskId(e), e);
  if (task) chips.push(task);
  const zones = eventZoneIds(e).map((z) => zoneSlug(state, z));
  for (const s of eventZoneSlugs(e)) if (!zones.includes(s)) zones.push(s);
  chips.push(...zones.slice(0, 2));
  return chips;
}

// ---------------------------------------------------------------------------
// Where a row leads (Enter / click): the exact screen for the item (§9.1)
// ---------------------------------------------------------------------------

export type RowTarget =
  | { screen: 'board'; task: string }
  | { screen: 'zones'; zone: string }
  | { screen: 'channel'; thread: string | null }
  | { screen: 'report'; report: string }
  | { screen: 'policy' }
  | { screen: 'agent'; agent: string; session: string | null };

export function rowTarget(e: CrewEvent, state: CrewState | null): RowTarget | null {
  const t = e.type;
  const report = payloadReport(e);
  const reportId = str(e.payload.report_id) ?? report?.id ?? e.refs.report_id ?? null;
  const taskId = eventTaskId(e);
  const zoneIds = eventZoneIds(e);
  const zone = zoneIds[0] ? zoneSlug(state, zoneIds[0]) : eventZoneSlugs(e)[0] ?? null;
  if (t === 'task.done' || t.startsWith('report.')) {
    if (reportId) return { screen: 'report', report: reportId };
  }
  if (t.startsWith('message.') || t.startsWith('decision.')) {
    const msg = payloadMessage(e);
    return { screen: 'channel', thread: msg ? msg.thread_root_id ?? msg.id : e.refs.message_id ?? null };
  }
  if (t === 'guard.bypass_used' || t === 'gate.tampered' || t === 'githook.missing' || t.startsWith('zone.change')) return { screen: 'policy' };
  if (t.startsWith('collision.') || t.startsWith('claim.') || t.startsWith('zone.') || t === 'guard.blocked' || t === 'guard.tamper_blocked') {
    if (zone) return { screen: 'zones', zone };
    if (t === 'guard.tamper_blocked') return { screen: 'policy' };
  }
  if (taskId && (t.startsWith('task.') || t.startsWith('checkpoint.') || t.startsWith('baton.') || t === 'handoff.created')) {
    return { screen: 'board', task: taskLabel(state, taskId, e) ?? taskId };
  }
  const sid = eventSessionId(e);
  const agent = e.actor.kind === 'session' ? e.actor.agent_id : sid ? state?.sessions[sid]?.agent_id : null;
  if (agent && (t.startsWith('session.') || t.startsWith('activity.') || t.startsWith('checkpoint.') || t.startsWith('baton.'))) {
    return { screen: 'agent', agent, session: sid };
  }
  if (taskId) return { screen: 'board', task: taskLabel(state, taskId, e) ?? taskId };
  if (zone) return { screen: 'zones', zone };
  return null;
}

// ---------------------------------------------------------------------------
// Time ("3s", "4m", "2h", then the clock in the viewer's zone)
// ---------------------------------------------------------------------------

export function ageText(ts: string | null | undefined, nowMs: number): string {
  if (!ts) return '';
  const at = Date.parse(ts);
  if (!Number.isFinite(at)) return '';
  const s = Math.max(0, Math.round((nowMs - at) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  return new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Windowing (the list is virtualised: only rows in view are in the DOM)
// ---------------------------------------------------------------------------

export interface WindowRange {
  start: number;
  /** Exclusive. */
  end: number;
  padTop: number;
  padBottom: number;
}

export function windowRange(scrollTop: number, viewport: number, count: number, rowH: number, overscan = 6): WindowRange {
  if (count <= 0 || rowH <= 0) return { start: 0, end: 0, padTop: 0, padBottom: 0 };
  const first = Math.floor(Math.max(0, scrollTop) / rowH);
  const visible = Math.ceil(Math.max(0, viewport) / rowH) + 1;
  const start = Math.max(0, Math.min(count - 1, first - overscan));
  const end = Math.min(count, first + visible + overscan);
  return { start, end, padTop: start * rowH, padBottom: Math.max(0, (count - end) * rowH) };
}

/** The scrollTop that brings row `index` fully into view, or null when it already is. */
export function scrollToReveal(index: number, scrollTop: number, viewport: number, rowH: number): number | null {
  const top = index * rowH;
  const bottom = top + rowH;
  if (top < scrollTop) return top;
  if (bottom > scrollTop + viewport) return Math.max(0, bottom - viewport);
  return null;
}

/** Move a selection by `delta` rows (j = +1 older, k = −1 newer), clamped; null selection starts at the top. */
export function moveSelection(rows: readonly FeedRow[], selected: string | null, delta: number): string | null {
  if (!rows.length) return null;
  const at = selected ? rows.findIndex((r) => r.key === selected) : -1;
  if (at < 0) return rows[0].key;
  const next = Math.max(0, Math.min(rows.length - 1, at + delta));
  return rows[next].key;
}
