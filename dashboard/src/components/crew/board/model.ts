// Task Board and receipt model (spec §9.7, §9.10, §5.4, §5.6). Pure functions
// only: the screens render what these return, and the tests pin the rules
// without a browser.
//
// Rules this file owns:
// - which column a task sits in (Up next, In progress, Blocked, Review, Done,
//   plus Stalled) and how swimlanes group it (Phase, Agent or Zone);
// - how the REST task list (every task, with timestamps and waivers) merges
//   with the live reducer state (open tasks from the snapshot, then events);
// - the acceptance meter, the per-item receipt seal with source labels
//   (observed / server-verified / self-reported), saved-work chips, zone
//   chips, checkpoint dots and dependency edges;
// - what a drag means: "No report means no Done" (dragging to Done opens the
//   report or waiver flow), stalled → pick up baton, done → reopen, anything
//   else is refused with the reason;
// - criteria validation that mirrors the server's (`validate_acceptance`).
//
// Text from agents (titles, summaries, blocked reasons) is returned as plain
// strings; the components render it as text, never as HTML (§9).

import type {
  ClaimMode,
  CheckpointView,
  CrewState,
  Criterion,
  FactsSource,
  ReportView,
  TaskStatus,
  TaskView,
} from '../../../lib/crew/types';
import { hrefFor } from '../../../lib/nav';
import { presenceText } from '../../../lib/crew/selectors';
import { parseServerTime } from '../../../lib/time';

// ---------------------------------------------------------------------------
// Wire shapes the board reads over REST (task_detail / report_detail on the server)
// ---------------------------------------------------------------------------

export interface Waiver {
  by?: string | null;
  reason?: string | null;
  at?: string | null;
}

export interface BoardCriterion extends Criterion {
  /** Present when a human waived this criterion (REST only). */
  waived?: Waiver | null;
}

/** `GET /crews/{id}/tasks` item: the TaskView plus bookkeeping fields. */
export interface TaskDetail extends Omit<TaskView, 'acceptance'> {
  acceptance: BoardCriterion[];
  crew_id?: string;
  ref?: string;
  body?: string | null;
  labels?: string[];
  owner_user_id?: string | null;
  started_at?: string | null;
  done_at?: string | null;
  stalled_at?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CriterionDetail {
  id: string;
  kind?: string | null;
  required?: boolean;
  status: 'met' | 'waived' | 'unmet' | 'unknown';
  source?: FactsSource | null;
  detail?: string | null;
}

export interface TestRun {
  fingerprint: string;
  passed: boolean;
  source?: FactsSource | null;
  at?: string | null;
}

export interface LiveCheck {
  criterion_id: string;
  url?: string | null;
  ok?: boolean | null;
  status?: number | null;
  error?: string | null;
}

export interface ReportDeploy {
  pushed?: boolean;
  pushed_source?: FactsSource | null;
  live?: LiveCheck[];
  seal?: string | null;
  reasons?: string[];
  waived_by?: string | null;
}

export interface Grounding {
  status: 'none' | 'consistent' | 'contradicted' | string;
  issues?: string[];
  checked?: string[];
}

/** `GET /tasks/{id}/reports` item (report_detail). */
export interface ReportDetail extends ReportView {
  crew_id?: string;
  criteria_detail?: CriterionDetail[];
  commits?: string[];
  files?: string[];
  out_of_zone_files?: string[];
  tests?: TestRun[];
  deploy?: ReportDeploy;
  sections?: Partial<Record<'done' | 'not_done' | 'failing' | 'next' | 'follow_ups', string[]>>;
  summary?: string | null;
  grounding?: Grounding | null;
  facts_hash?: string | null;
  reviewed_by?: string | null;
  review_note?: string | null;
  seal?: string | null;
  created_at?: string | null;
}

/** `GET /crews/{id}/checkpoints` item (checkpoint_detail). */
export interface CheckpointDetail extends CheckpointView {
  created_at?: string | null;
  seq?: number | null;
}

/** `GET /crews/{id}/batons` item. */
export interface BatonRow {
  id: string;
  task_id?: string | null;
  from_session?: string | null;
  to_session: string;
  from_callsign?: string | null;
  to_callsign?: string | null;
  kind: string;
  handoff_id?: string | null;
  checkpoint_id?: string | null;
  report_id?: string | null;
  baton_ref?: string | null;
  restored?: boolean | null;
  zone_ids?: string[];
  brief_text?: string | null;
  seq?: number | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Columns and swimlanes
// ---------------------------------------------------------------------------

export type ColumnId = 'next' | 'progress' | 'blocked' | 'review' | 'done' | 'stalled';

export interface BoardColumn {
  id: ColumnId;
  title: string;
  statuses: readonly TaskStatus[];
  /** What the column means, for the column header's accessible description. */
  hint: string;
}

export const BOARD_COLUMNS: readonly BoardColumn[] = [
  { id: 'next', title: 'Up next', statuses: ['backlog', 'ready', 'claimed'], hint: 'Backlog, ready, and claimed but not started' },
  { id: 'progress', title: 'In progress', statuses: ['in_progress'], hint: 'An agent is working on it' },
  { id: 'blocked', title: 'Blocked', statuses: ['blocked'], hint: 'Waiting on something outside the task' },
  { id: 'review', title: 'Review', statuses: ['review'], hint: 'A report is waiting for a person' },
  { id: 'done', title: 'Done', statuses: ['done'], hint: 'Closed with a report or a human waiver' },
  { id: 'stalled', title: 'Stalled', statuses: ['stalled'], hint: 'The owner stopped; the baton is waiting for a pickup' },
];

export const COLUMN_IDS: readonly ColumnId[] = BOARD_COLUMNS.map((c) => c.id);

export function columnOf(status: TaskStatus, showCancelled = false): ColumnId | null {
  if (status === 'cancelled') return showCancelled ? 'done' : null;
  for (const col of BOARD_COLUMNS) if (col.statuses.includes(status)) return col.id;
  return null;
}

export function columnTitle(id: ColumnId): string {
  return BOARD_COLUMNS.find((c) => c.id === id)?.title ?? id;
}

export const STATUS_TEXT: Record<TaskStatus, string> = {
  backlog: 'backlog',
  ready: 'ready',
  claimed: 'claimed',
  in_progress: 'in progress',
  blocked: 'blocked',
  review: 'in review',
  done: 'done',
  stalled: 'stalled',
  cancelled: 'cancelled',
};

export type SwimlaneBy = 'phase' | 'agent' | 'zone' | 'none';
export const SWIMLANES: readonly { id: SwimlaneBy; label: string }[] = [
  { id: 'phase', label: 'Phase' },
  { id: 'agent', label: 'Agent' },
  { id: 'zone', label: 'Zone' },
  { id: 'none', label: 'None' },
];

export function isSwimlane(value: string | null | undefined): value is SwimlaneBy {
  return value === 'phase' || value === 'agent' || value === 'zone' || value === 'none';
}

export interface Lane {
  key: string;
  title: string;
  /** Mono sub-label, e.g. "key-verified" for an agent lane. */
  note: string | null;
  cells: Record<ColumnId, TaskDetail[]>;
  count: number;
}

function emptyCells(): Record<ColumnId, TaskDetail[]> {
  return { next: [], progress: [], blocked: [], review: [], done: [], stalled: [] };
}

/** Merge the REST list with live reducer tasks: the higher `version` wins field by field it carries. */
export function mergeTasks(rest: readonly TaskDetail[] | null | undefined, live: Record<string, TaskView>): TaskDetail[] {
  const byId = new Map<string, TaskDetail>();
  for (const t of rest ?? []) byId.set(t.id, t);
  for (const t of Object.values(live)) {
    const known = byId.get(t.id);
    if (!known) {
      byId.set(t.id, { ...t });
      continue;
    }
    if (t.version < known.version) continue;
    if (t.version === known.version) {
      // Same version: the REST detail is a superset (waivers, timestamps).
      continue;
    }
    // Live is newer: take its fields; keep REST-only fields, and keep a waiver
    // only if the criterion is unchanged (a newer version may have replaced it).
    const oldWaivers = new Map(known.acceptance.map((c) => [c.id, c]));
    const acceptance = t.acceptance.map((c) => {
      const old = oldWaivers.get(c.id);
      return old && old.waived && old.text === c.text && old.kind === c.kind ? { ...c, waived: old.waived } : { ...c };
    });
    byId.set(t.id, { ...known, ...t, acceptance });
  }
  return [...byId.values()];
}

/** True when some live task is newer than the REST list (the list should be refetched). */
export function restIsStale(rest: readonly TaskDetail[] | null | undefined, live: Record<string, TaskView>): boolean {
  if (!rest) return false;
  const versions = new Map(rest.map((t) => [t.id, t.version]));
  for (const t of Object.values(live)) {
    const v = versions.get(t.id);
    if (v === undefined || t.version > v) return true;
  }
  return false;
}

function statusRank(t: TaskDetail): number {
  return t.status === 'claimed' ? 0 : t.status === 'ready' ? 1 : t.status === 'backlog' ? 2 : 0;
}

/** Order inside a cell: priority (0 first), then position, then number; Done newest first. */
export function sortCell(column: ColumnId, tasks: TaskDetail[]): TaskDetail[] {
  const copy = [...tasks];
  if (column === 'done') {
    return copy.sort((a, b) => {
      const ta = parseServerTime(a.done_at)?.getTime() ?? 0;
      const tb = parseServerTime(b.done_at)?.getTime() ?? 0;
      return tb - ta || b.number - a.number;
    });
  }
  return copy.sort(
    (a, b) => statusRank(a) - statusRank(b) || (a.priority ?? 2) - (b.priority ?? 2) || a.number - b.number,
  );
}

export interface OwnerInfo {
  /** "cc-1", or the agent id, or "unassigned". */
  label: string;
  callsign: string | null;
  agentId: string | null;
  verified: boolean | null;
  live: boolean;
  /** Presence worth saying on a card ("stopped (billing_error)", "quiet", "paused"); null when working. */
  note: string | null;
}

const LIVE_STATES = new Set(['joining', 'active', 'idle', 'quiet', 'quota_blocked', 'paused']);

export function ownerOf(task: Pick<TaskDetail, 'owner_session_id' | 'owner_agent_id'>, state: CrewState | null): OwnerInfo {
  const session = task.owner_session_id && state ? state.sessions[task.owner_session_id] : undefined;
  if (session) {
    return {
      label: session.callsign,
      callsign: session.callsign,
      agentId: session.agent_id,
      verified: session.agent_verified,
      live: LIVE_STATES.has(session.state),
      note: session.state === 'active' || session.state === 'idle' || session.state === 'joining' ? (session.stuck ? 'stuck' : null) : presenceText(session),
    };
  }
  if (task.owner_agent_id) return { label: task.owner_agent_id, callsign: null, agentId: task.owner_agent_id, verified: null, live: false, note: null };
  return { label: 'unassigned', callsign: null, agentId: null, verified: null, live: false, note: null };
}

function laneOf(task: TaskDetail, by: SwimlaneBy, state: CrewState | null): { key: string; title: string; note: string | null } {
  if (by === 'none') return { key: 'all', title: 'All tasks', note: null };
  if (by === 'phase') {
    const phase = task.phase?.trim();
    return phase ? { key: `p:${phase}`, title: phase, note: null } : { key: 'p:', title: 'No phase', note: null };
  }
  if (by === 'agent') {
    const owner = ownerOf(task, state);
    if (!owner.agentId) return { key: 'a:', title: 'Unassigned', note: null };
    const note = owner.verified === null ? null : owner.verified ? 'key-verified' : 'self-declared';
    return { key: `a:${owner.label}`, title: owner.label, note };
  }
  const zoneId = task.zone_ids[0];
  if (!zoneId) return { key: 'z:', title: 'No zone', note: null };
  const zone = state?.zones[zoneId];
  return { key: `z:${zoneId}`, title: zone ? zone.slug : zoneId, note: zone?.title && zone.title !== zone.slug ? zone.title : null };
}

/** Group tasks into swimlanes × columns. Empty lanes are dropped; the catch-all lane sorts last. */
export function buildLanes(
  tasks: readonly TaskDetail[],
  by: SwimlaneBy,
  state: CrewState | null,
  options: { showCancelled?: boolean } = {},
): Lane[] {
  const lanes = new Map<string, Lane & { order: number }>();
  for (const task of tasks) {
    const column = columnOf(task.status, options.showCancelled);
    if (!column) continue;
    const meta = laneOf(task, by, state);
    let lane = lanes.get(meta.key);
    if (!lane) {
      lane = { ...meta, cells: emptyCells(), count: 0, order: Number.MAX_SAFE_INTEGER };
      lanes.set(meta.key, lane);
    }
    lane.cells[column].push(task);
    lane.count += 1;
    lane.order = Math.min(lane.order, task.number);
  }
  const catchAll = new Set(['p:', 'a:', 'z:']);
  return [...lanes.values()]
    .sort((a, b) => Number(catchAll.has(a.key)) - Number(catchAll.has(b.key)) || a.order - b.order || a.title.localeCompare(b.title))
    .map((lane): Lane => {
      const cells = emptyCells();
      for (const id of COLUMN_IDS) cells[id] = sortCell(id, lane.cells[id]);
      return { key: lane.key, title: lane.title, note: lane.note, cells, count: lane.count };
    });
}

export function columnCounts(tasks: readonly TaskDetail[], showCancelled = false): Record<ColumnId, number> {
  const counts: Record<ColumnId, number> = { next: 0, progress: 0, blocked: 0, review: 0, done: 0, stalled: 0 };
  for (const t of tasks) {
    const c = columnOf(t.status, showCancelled);
    if (c) counts[c] += 1;
  }
  return counts;
}

// ---------------------------------------------------------------------------
// Acceptance meter, sources and the receipt seal
// ---------------------------------------------------------------------------

export const SOURCE_LABEL: Record<FactsSource, string> = {
  'relay-cli': 'observed',
  'server-verified': 'server-verified',
  'agent-declared': 'self-reported',
  'server-inferred': 'inferred',
};

export function sourceLabel(source: FactsSource | string | null | undefined): string {
  if (!source) return 'no evidence';
  return (SOURCE_LABEL as Record<string, string>)[source] ?? source;
}

/** Strong evidence (observed by a hook/CLI, or verified by the server) vs self-reported. */
export function isStrongSource(source: FactsSource | string | null | undefined): boolean {
  return source === 'relay-cli' || source === 'server-verified';
}

export type CriterionState = 'met' | 'waived' | 'unmet' | 'unknown';

export interface MeterItem {
  id: string;
  required: boolean;
  state: CriterionState;
  source: FactsSource | null;
}

export interface AcceptanceMeter {
  items: MeterItem[];
  /** Required criteria met or waived, over required criteria. */
  done: number;
  required: number;
  met: number;
  waived: number;
  unmet: number;
  /** True when the numbers come from a report (otherwise only waivers are known). */
  reported: boolean;
}

type ReportCriteria = Pick<ReportView, 'criteria'> | null | undefined;

export function acceptanceMeter(task: Pick<TaskDetail, 'acceptance'>, report: ReportCriteria): AcceptanceMeter {
  const results = new Map((report?.criteria ?? []).map((c) => [c.id, c]));
  const items: MeterItem[] = task.acceptance.map((c) => {
    const r = results.get(c.id);
    let state: CriterionState = r ? r.status : 'unknown';
    if (c.waived) state = 'waived';
    return { id: c.id, required: c.required !== false, state, source: (r?.source ?? null) as FactsSource | null };
  });
  const req = items.filter((i) => i.required);
  return {
    items,
    required: req.length,
    done: req.filter((i) => i.state === 'met' || i.state === 'waived').length,
    met: items.filter((i) => i.state === 'met').length,
    waived: items.filter((i) => i.state === 'waived').length,
    unmet: items.filter((i) => i.state === 'unmet').length,
    reported: !!report,
  };
}

export type SealGroup = 'tests' | 'commands' | 'commits' | 'files' | 'manual' | 'pushed' | 'live' | 'other';
export type SealMark = 'ok' | 'fail' | 'waived' | 'unknown';

export interface SealItem {
  group: SealGroup;
  mark: SealMark;
  /** The weakest source among the met items (the label a person should trust least). */
  source: FactsSource | null;
  /** "tests ✓ (observed)" — identical to the server's receipt seal text. */
  text: string;
}

const SEAL_GROUP_OF: Record<string, SealGroup> = {
  test: 'tests',
  command: 'commands',
  commit: 'commits',
  file: 'files',
  deploy: 'live',
  manual: 'manual',
};

function sealPart(group: SealGroup, items: { status: string; source?: string | null }[]): SealItem {
  const statuses = items.map((i) => i.status);
  if (statuses.some((s) => s === 'unmet')) return { group, mark: 'fail', source: null, text: `${group} ✗` };
  if (statuses.every((s) => s === 'waived')) return { group, mark: 'waived', source: null, text: `${group} waived` };
  if (statuses.every((s) => s === 'met' || s === 'waived')) {
    const sources = items.filter((i) => i.status === 'met').map((i) => String(i.source));
    const weakest: FactsSource = sources.includes('agent-declared')
      ? 'agent-declared'
      : sources.includes('relay-cli')
        ? 'relay-cli'
        : 'server-verified';
    return { group, mark: 'ok', source: weakest, text: `${group} ✓ (${SOURCE_LABEL[weakest]})` };
  }
  return { group, mark: 'unknown', source: null, text: `${group} ?` };
}

/**
 * The receipt seal, one item per evidence group, in the server's order
 * (tests · commands · commits · files · manual · pushed · live). Mirrors
 * `remembra.crew.reports.receipt_seal`, so `sealText(sealItems(r))` equals the
 * server's `seal` for any gate-produced report.
 */
export function sealItems(report: ReportDetail | ReportView, acceptance: readonly Criterion[] = []): SealItem[] {
  const detail = report as ReportDetail;
  const kinds = new Map(acceptance.map((c) => [c.id, c.kind]));
  const rows: { kind: string; status: string; source?: string | null }[] = (
    detail.criteria_detail ?? report.criteria.map((c) => ({ ...c, kind: kinds.get(c.id) ?? null }))
  ).map((c) => ({ kind: String(c.kind ?? kinds.get(c.id) ?? 'other'), status: c.status, source: c.source ?? null }));
  const groups = new Map<SealGroup, typeof rows>();
  for (const row of rows) {
    const g = SEAL_GROUP_OF[row.kind] ?? 'other';
    groups.set(g, [...(groups.get(g) ?? []), row]);
  }
  const out: SealItem[] = [];
  for (const name of ['tests', 'commands', 'commits', 'files', 'manual'] as const) {
    const items = groups.get(name);
    if (items) out.push(sealPart(name, items));
  }
  const deploy = detail.deploy;
  if (deploy) {
    const requirePushed = (deploy.reasons ?? []).includes('not pushed');
    if (deploy.pushed) {
      const src = (deploy.pushed_source ?? 'relay-cli') as FactsSource;
      out.push({ group: 'pushed', mark: 'ok', source: src, text: `pushed ✓ (${sourceLabel(src)})` });
    } else if (requirePushed) {
      out.push({ group: 'pushed', mark: 'fail', source: null, text: 'pushed ✗' });
    }
  }
  const live = groups.get('live');
  if (live) out.push(sealPart('live', live));
  return out;
}

export function sealText(items: readonly SealItem[]): string {
  return items.map((i) => i.text).join(' · ');
}

/** The one-line seal a Done card shows: the server's text when present, else computed. */
export function sealLine(report: ReportDetail | ReportView, acceptance: readonly Criterion[] = []): string {
  if (report.kind === 'waived') return 'waived by a human';
  const detail = report as ReportDetail;
  if (detail.seal) return detail.seal;
  return sealText(sealItems(report, acceptance)) || (report.verdict ? `verdict ${report.verdict}` : 'no criteria');
}

// ---------------------------------------------------------------------------
// Card details: zones, saved work, checkpoints, dependencies, age
// ---------------------------------------------------------------------------

export interface ZoneChipInfo {
  zoneId: string;
  slug: string;
  mode: ClaimMode;
  /** Held for the next pickup (the baton). */
  reserved: boolean;
  /** Inherited through a baton (adopted or handed over). */
  inherited: boolean;
  frozen: boolean;
}

export function zoneChips(task: Pick<TaskDetail, 'id' | 'zone_ids'>, state: CrewState | null): ZoneChipInfo[] {
  return task.zone_ids.map((zoneId) => {
    const zone = state?.zones[zoneId];
    const claim = state
      ? Object.values(state.claims).find(
          (c) => c.zone_id === zoneId && c.task_id === task.id && ['active', 'offered', 'reserved', 'queued', 'requested'].includes(c.state),
        )
      : undefined;
    return {
      zoneId,
      slug: zone?.slug ?? zoneId,
      mode: claim?.mode ?? zone?.mode ?? 'exclusive',
      reserved: claim?.state === 'reserved',
      inherited: claim ? ['adopt', 'handover'].includes(claim.source) : false,
      frozen: !!zone?.frozen_by,
    };
  });
}

export interface SavedWork {
  ref: string;
  dirtyFiles: number | null;
  unpushed: number | null;
}

/** The newest baton ref carrying this task's uncommitted work ("saved work" chip). */
export function savedWork(task: Pick<TaskDetail, 'id'>, state: CrewState | null): SavedWork | null {
  if (!state) return null;
  let best: (SavedWork & { seq: number }) | null = null;
  for (const [ref, entry] of Object.entries(state.baton_refs)) {
    if (entry.task_id !== task.id) continue;
    if (!best || entry.seq > best.seq) best = { ref, dirtyFiles: entry.dirty_files, unpushed: entry.unpushed, seq: entry.seq };
  }
  if (best) return { ref: best.ref, dirtyFiles: best.dirtyFiles, unpushed: best.unpushed };
  const claim = Object.values(state.claims).find((c) => c.task_id === task.id && c.baton_ref && c.state === 'reserved');
  return claim?.baton_ref ? { ref: claim.baton_ref, dirtyFiles: null, unpushed: null } : null;
}

export interface CheckpointDot {
  id: string;
  trigger: string;
  headline: string;
  at: string | null;
  /** Within the last 10 minutes. */
  recent: boolean;
}

/** The task's latest checkpoints, oldest → newest (at most `max`). */
export function checkpointDots(
  checkpoints: readonly CheckpointDetail[],
  taskId: string,
  now: Date,
  max = 6,
): CheckpointDot[] {
  const mine = checkpoints
    .filter((c) => c.task_id === taskId)
    .map((c) => ({ c, t: parseServerTime(c.created_at)?.getTime() ?? 0 }))
    .sort((a, b) => a.t - b.t)
    .slice(-max);
  return mine.map(({ c, t }) => ({
    id: c.id,
    trigger: c.trigger,
    headline: c.headline,
    at: c.created_at ?? null,
    recent: t > 0 && now.getTime() - t < 10 * 60_000,
  }));
}

/** Add live checkpoints (latest per session from the reducer) to the loaded list, deduplicated by id. */
export function mergeCheckpoints(
  loaded: readonly CheckpointDetail[] | null | undefined,
  live: Record<string, CheckpointView>,
  seenAt: Record<string, string>,
): CheckpointDetail[] {
  const byId = new Map<string, CheckpointDetail>();
  for (const c of loaded ?? []) byId.set(c.id, c);
  for (const c of Object.values(live)) {
    if (!byId.has(c.id)) byId.set(c.id, { ...c, created_at: seenAt[c.id] ?? null });
  }
  return [...byId.values()];
}

export interface DependencyEdge {
  /** The task that must finish first. */
  from: string;
  /** The task waiting on it. */
  to: string;
  satisfied: boolean;
}

export function dependencyEdges(tasks: readonly TaskDetail[]): DependencyEdge[] {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const edges: DependencyEdge[] = [];
  for (const t of tasks) {
    for (const dep of t.depends_on) {
      const d = byId.get(dep);
      if (d) edges.push({ from: dep, to: t.id, satisfied: d.status === 'done' });
    }
  }
  return edges;
}

export interface DepRef {
  id: string;
  ref: string;
  done: boolean;
}

export function dependencyRefs(task: Pick<TaskDetail, 'depends_on'>, byId: ReadonlyMap<string, TaskDetail>): DepRef[] {
  return task.depends_on.map((id) => {
    const d = byId.get(id);
    return { id, ref: d ? `T-${d.number}` : id, done: d?.status === 'done' };
  });
}

/** When the task entered its current status, as far as the server says. */
export function statusSince(task: TaskDetail, localSince: Readonly<Record<string, string>> = {}): string | null {
  const local = localSince[task.id];
  let since: string | null | undefined;
  switch (task.status) {
    case 'done':
      since = task.done_at;
      break;
    case 'stalled':
      since = task.stalled_at;
      break;
    case 'in_progress':
      since = task.started_at;
      break;
    default:
      since = task.updated_at ?? task.created_at;
  }
  const a = parseServerTime(since ?? null)?.getTime() ?? 0;
  const b = parseServerTime(local ?? null)?.getTime() ?? 0;
  if (b > a) return local ?? null;
  return since ?? local ?? null;
}

/** Compact age: "40s", "12m", "3h", "2d". */
export function ageText(since: string | null, now: Date): string | null {
  const t = parseServerTime(since)?.getTime();
  if (t === undefined) return null;
  const s = Math.max(0, Math.round((now.getTime() - t) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

// ---------------------------------------------------------------------------
// Live movement (for the packet trail and the status strip)
// ---------------------------------------------------------------------------

export interface StatusMove {
  taskId: string;
  number: number;
  from: ColumnId | null;
  to: ColumnId | null;
  toStatus: TaskStatus;
}

/** Tasks whose column changed between two reducer states. New tasks count as a move into their column. */
export function statusMoves(prev: Record<string, TaskView> | null, next: Record<string, TaskView>): StatusMove[] {
  if (!prev) return [];
  const moves: StatusMove[] = [];
  for (const t of Object.values(next)) {
    const before = prev[t.id];
    if (before && before.status === t.status) continue;
    const from = before ? columnOf(before.status, true) : null;
    const to = columnOf(t.status, true);
    if (from === to && before) continue;
    moves.push({ taskId: t.id, number: t.number, from, to, toStatus: t.status });
  }
  return moves;
}

// ---------------------------------------------------------------------------
// What a drag (or the card menu) means
// ---------------------------------------------------------------------------

export type DropIntent =
  | { kind: 'none' }
  | { kind: 'report'; mode: 'review' | 'waive' }
  | { kind: 'pickup' }
  | { kind: 'reopen' }
  | { kind: 'refused'; reason: string };

export const AGENT_MOVES_REASON =
  'Agents move their own tasks (claim, start, block). From here you can close a task with its report or a waiver, pick up a stalled baton, or reopen a done task.';

/** "No report means no Done" (§9.7): a drop never sets a status directly. */
export function dropIntent(task: Pick<TaskDetail, 'status'>, to: ColumnId, human: boolean): DropIntent {
  const from = columnOf(task.status, true);
  if (from === to) return { kind: 'none' };
  if (!human) return { kind: 'refused', reason: 'Only a dashboard login can move tasks; API keys are never a person.' };
  if (task.status === 'cancelled') return { kind: 'refused', reason: 'This task was cancelled.' };
  if (to === 'done') {
    if (task.status === 'review') return { kind: 'report', mode: 'review' };
    return { kind: 'report', mode: 'waive' };
  }
  if (task.status === 'stalled' && (to === 'progress' || to === 'next')) return { kind: 'pickup' };
  if (task.status === 'done' && to === 'next') return { kind: 'reopen' };
  return { kind: 'refused', reason: AGENT_MOVES_REASON };
}

/** The drop targets that do something for a task (for highlighting while dragging). */
export function dropTargets(task: Pick<TaskDetail, 'status'>, human: boolean): ColumnId[] {
  return COLUMN_IDS.filter((c) => {
    const intent = dropIntent(task, c, human);
    return intent.kind !== 'refused' && intent.kind !== 'none';
  });
}

// ---------------------------------------------------------------------------
// Routing helpers
// ---------------------------------------------------------------------------

/** Resolve a `task=` URL value: a task id, or "T-14" / "14". */
export function resolveTaskParam(param: string | null | undefined, tasks: readonly TaskDetail[]): TaskDetail | null {
  if (!param) return null;
  const byId = tasks.find((t) => t.id === param);
  if (byId) return byId;
  const m = /^T?-?(\d+)$/i.exec(param.trim());
  if (!m) return null;
  const n = Number(m[1]);
  return tasks.find((t) => t.number === n) ?? null;
}

export function boardHref(project: string, task?: string | null, lanes?: SwimlaneBy | null): string {
  return hrefFor('crew', { project, view: 'board', task: task ?? null, lanes: lanes && lanes !== 'phase' ? lanes : null });
}

/** A receipt link; the task id makes a deep link to an older (superseded) report resolvable. */
export function receiptHref(project: string, reportId: string, taskId?: string | null): string {
  return hrefFor('crew', { project, view: 'report', report: reportId, task: taskId ?? null });
}

/** The task a report belongs to, from what is already on screen (null = look it up). */
export function taskForReport(reportId: string, state: CrewState | null, tasks: readonly TaskDetail[]): string | null {
  if (state) {
    for (const r of Object.values(state.reports)) if (r.id === reportId) return r.task_id;
  }
  const t = tasks.find((x) => x.current_report_id === reportId);
  return t ? t.id : null;
}

// ---------------------------------------------------------------------------
// Criteria editing (mirrors the server's validate_acceptance, §5.4)
// ---------------------------------------------------------------------------

export const CRITERION_KINDS = ['test', 'command', 'file', 'commit', 'deploy', 'manual'] as const;
export const MAX_CRITERIA = 20;
const CRITERION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
/** A repo-relative POSIX path (the server's PATH_REL_PATTERN): no leading "/" or "~", no ".." segment, no backslash or control character. */
export function isPathRel(path: string): boolean {
  if (!path || path.length > 1024 || path.startsWith('/') || path.startsWith('~') || path.includes('\\')) return false;
  if (path.split('/').some((seg) => seg === '..')) return false;
  for (let i = 0; i < path.length; i += 1) if (path.charCodeAt(i) < 0x20) return false;
  return true;
}
const HTTPS = /^https:\/\/[^\s]{1,500}$/;
const COMMAND_TOKEN = /^[A-Za-z0-9._:/@=+,%-]{1,64}$/;

/** Problems with an argv-prefix pattern (D38), as the server's validate_command_pattern reports them. */
export function commandPatternProblems(pattern: string): string[] {
  if (!pattern.trim()) return ['the pattern is empty'];
  if (pattern.length > 256) return ['the pattern is longer than 256 characters'];
  if (pattern !== pattern.trim() || /\s{2,}|\t|\n/.test(pattern)) return ['separate words with single spaces'];
  const tokens = pattern.split(' ');
  const out: string[] = [];
  if (tokens.length > 16) out.push('at most 16 words');
  if (tokens[0] === '*') out.push('start with the program name, not "*"');
  for (const tok of tokens) {
    if (tok === '*') continue;
    if (tok.includes('*')) out.push(`"${tok}": "*" must be a whole word`);
    else if (!COMMAND_TOKEN.test(tok)) out.push(`"${tok}": only letters, digits and ._:/@=+,%- (no regex)`);
  }
  return out;
}

export interface CriterionDraft {
  id: string;
  text: string;
  kind: Criterion['kind'];
  match: string;
  url: string;
  required: boolean;
}

export function toDraft(c: Criterion): CriterionDraft {
  return { id: c.id, text: c.text, kind: c.kind, match: c.match ?? '', url: c.url ?? '', required: c.required !== false };
}

export function fromDraft(d: CriterionDraft): Criterion {
  const needsMatch = d.kind === 'test' || d.kind === 'command' || d.kind === 'file' || d.kind === 'commit';
  return {
    id: d.id.trim(),
    text: d.text.trim(),
    kind: d.kind,
    match: needsMatch && d.match.trim() ? d.match.trim() : null,
    url: d.kind === 'deploy' && d.url.trim() ? d.url.trim() : null,
    required: d.required,
  };
}

export function nextCriterionId(existing: readonly { id: string }[]): string {
  const used = new Set(existing.map((c) => c.id));
  for (let n = existing.length + 1; ; n += 1) if (!used.has(`c${n}`)) return `c${n}`;
}

export function emptyDraft(existing: readonly { id: string }[]): CriterionDraft {
  return { id: nextCriterionId(existing), text: '', kind: 'test', match: '', url: '', required: true };
}

/** Problems with a criteria list, one line each (empty = valid). */
export function validateCriteria(drafts: readonly CriterionDraft[]): string[] {
  const errors: string[] = [];
  if (drafts.length > MAX_CRITERIA) errors.push(`At most ${MAX_CRITERIA} criteria.`);
  const seen = new Set<string>();
  drafts.forEach((d, i) => {
    const where = d.id.trim() || `#${i + 1}`;
    const id = d.id.trim();
    if (!CRITERION_ID.test(id)) errors.push(`${where}: the id must be 1–32 letters, digits, "-" or "_".`);
    else if (seen.has(id)) errors.push(`${where}: the id is used twice.`);
    seen.add(id);
    const text = d.text.trim();
    if (!text) errors.push(`${where}: describe what "done" means.`);
    else if (text.length > 280) errors.push(`${where}: at most 280 characters.`);
    const match = d.match.trim();
    if (d.kind === 'test' || d.kind === 'command') {
      if (!match) errors.push(`${where}: a ${d.kind} criterion needs the command it is matched against (e.g. "npm test -- pos").`);
      else for (const p of commandPatternProblems(match)) errors.push(`${where}: ${p}.`);
    }
    if (d.kind === 'file' && (!match || !isPathRel(match))) errors.push(`${where}: a file criterion needs a repo-relative path.`);
    if (d.kind === 'commit' && match && !/^[0-9a-f]{7,40}$/.test(match.toLowerCase())) errors.push(`${where}: a commit criterion takes a sha prefix (7–40 hex).`);
    if (d.kind === 'deploy' && !HTTPS.test(d.url.trim())) errors.push(`${where}: a deploy criterion needs an https URL.`);
  });
  return errors;
}

export const WAIVER_REASON_MAX = 280;

export function waiverReasonError(reason: string): string | null {
  const r = reason.trim();
  if (!r) return 'Say why. The reason is kept on the receipt and in the audit log.';
  if (r.length > WAIVER_REASON_MAX) return `At most ${WAIVER_REASON_MAX} characters.`;
  return null;
}
