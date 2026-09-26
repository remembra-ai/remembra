// The Site Board build tree (spec §9.2): one mono tree per project, phases at
// the top level, the agents working inside each phase under it, and batons
// waiting for pickup where their task sits.
//
//   ├─ ✓ Phase 1  Invoices + GCT             12/12
//   ├─ ◉ Phase 2  POS                         5/9
//   │  ├─ ◉ [CC] cc-1  cart → receipts        POS ▨ excl · enforced
//   │  ├─ ◌ [CX] codex-1  receipt tests       ⧗ waiting on POS
//   │  └─ ⚠ [CC] cc-2  —                      ✦ baton ready (3 files saved)
//   └─ ○ Phase 3  Payroll                     0/6
//
// Data: `GET /crews` (phases with done/total, live sessions) plus, when it has
// loaded, the crew's snapshot (open tasks with their phase, claims, zones,
// baton offers) so each agent sits under the phase of its task.

import { agentMeta } from '../../lib/agents';
import { liveSessions } from '../../lib/crew/selectors';
import { fromSnapshot } from '../../lib/crew/reducer';
import type { CrewListItem, CrewSnapshot, CrewState, SessionView } from '../../lib/crew/types';
import { enforcementView, presenceView, sessionClaims } from '../../components/crew/lane/model';
import { pickupSlots, type PickupSlotView } from '../../components/crew/lane/pickup';

export type PhaseGlyph = '✓' | '◉' | '○';
export type RowGlyph = '◉' | '◌' | '⚠' | '⏸' | '✦';

export interface SessionRow {
  kind: 'session';
  key: string;
  glyph: RowGlyph;
  /** What the glyph means (status never depends on the glyph alone). */
  status: string;
  session: SessionView;
  monogram: string;
  taskRef: string | null;
  /** Untrusted task title (plain text). */
  taskTitle: string | null;
  /** "POS ▨ excl · enforced", "⧗ waiting on POS", "fenced" … */
  right: string;
  alarm: boolean;
}

export interface BatonRow {
  kind: 'baton';
  key: string;
  glyph: '✦';
  status: string;
  slot: PickupSlotView;
  right: string;
}

export type TreeLeaf = SessionRow | BatonRow;

export interface PhaseNode {
  key: string;
  label: string;
  glyph: PhaseGlyph;
  status: string;
  done: number;
  total: number;
  children: TreeLeaf[];
}

export interface BuildTree {
  phases: PhaseNode[];
  /** Agents with no task (or a task outside the known phases) and non-task batons. */
  loose: TreeLeaf[];
  live: number;
  /** True when the tree is built from the list alone (the snapshot is still loading). */
  partial: boolean;
}

const MODE_SHORT = { exclusive: 'excl', shared: 'shared', watch: 'watch' } as const;
const MODE_GLYPH = { exclusive: '▨', shared: '▧', watch: '▢' } as const;

function phaseKey(phase: string | null | undefined): string {
  return phase ?? '';
}

function sessionRow(state: CrewState | null, session: SessionView, nowMs: number): SessionRow {
  const withPresence = { ...session, presence: null };
  const claims = state ? sessionClaims(state, session.id) : [];
  const presence = presenceView(withPresence, claims, nowMs);
  const task = state && session.current_task_id ? (state.tasks[session.current_task_id] ?? null) : null;
  const held = claims.filter((c) => c.state === 'active' || c.state === 'offered');
  const waiting = claims.filter((c) => c.state === 'queued' || c.state === 'requested');
  const slug = (zoneId: string | null | undefined, fallback: string) => (zoneId && state?.zones[zoneId]?.slug.toUpperCase()) || fallback;
  const parts: string[] = [];
  for (const c of held) parts.push(`${slug(c.zone_id, c.resource ?? 'claim')} ${MODE_GLYPH[c.mode]} ${MODE_SHORT[c.mode]}`);
  for (const c of waiting) parts.push(`⧗ waiting on ${slug(c.zone_id, c.resource ?? 'a claim')}`);
  if (presence.fenced) parts.push('fenced');
  const enforcement = enforcementView(session);
  if (held.length || waiting.length) parts.push(enforcement.alarm ? 'commit gate: missing' : enforcement.beforeWrite);
  else if (enforcement.alarm) parts.push('commit gate: missing');

  let glyph: RowGlyph = '◉';
  if (session.state === 'paused') glyph = '⏸';
  else if (presence.settled || session.stuck) glyph = '⚠';
  else if (session.state !== 'active' || (waiting.length && !held.length)) glyph = '◌';
  const label = presence.detail ? `${presence.label} (${presence.detail})` : presence.label;
  return {
    kind: 'session',
    key: session.id,
    glyph,
    status: session.stuck ? `${label}, stuck` : label,
    session,
    monogram: agentMeta(session.agent_id).monogram,
    taskRef: task ? `T-${task.number}` : null,
    taskTitle: task?.title ?? null,
    right: parts.join(' · '),
    alarm: presence.settled || session.stuck || enforcement.alarm,
  };
}

function batonRow(slot: PickupSlotView, nowMs: number): BatonRow {
  const bits: string[] = [];
  if (slot.savedFiles) bits.push(`${slot.savedFiles} file${slot.savedFiles === 1 ? '' : 's'} saved`);
  else if (slot.claims.some((c) => c.baton_ref)) bits.push('work saved');
  const head = `✦ baton ready${bits.length ? ` (${bits.join(', ')})` : ''}`;
  const ago = slot.sinceMs !== null ? Math.max(0, Math.round((nowMs - slot.sinceMs) / 60000)) : null;
  const when = ago === null ? '' : ago < 1 ? ' just now' : ago < 60 ? ` ${ago}m ago` : ` ${Math.round(ago / 60)}h ago`;
  return {
    kind: 'baton',
    key: slot.key,
    glyph: '✦',
    status: `baton waiting for pickup: ${slot.reasonText}`,
    slot,
    right: `${head}  ${slot.reasonText}${when}`,
  };
}

export function buildTree(item: CrewListItem, snapshot: CrewSnapshot | null, nowMs: number): BuildTree {
  const state = snapshot ? fromSnapshot(snapshot) : null;
  const sessions: SessionView[] = state ? liveSessions(state) : item.live_sessions;
  const phases = new Map<string, PhaseNode>();
  for (const p of item.phases) {
    const key = phaseKey(p.phase);
    const glyph: PhaseGlyph = p.total > 0 && p.done >= p.total ? '✓' : '○';
    phases.set(key, {
      key,
      label: p.phase ?? 'Tasks',
      glyph,
      status: glyph === '✓' ? 'done' : 'not started',
      done: p.done,
      total: p.total,
      children: [],
    });
  }
  const loose: TreeLeaf[] = [];
  const place = (phase: string | null | undefined, leaf: TreeLeaf) => {
    const node = phase !== undefined ? phases.get(phaseKey(phase)) : undefined;
    if (node) node.children.push(leaf);
    else loose.push(leaf);
  };
  for (const session of sessions) {
    const row = sessionRow(state, session, nowMs);
    const task = state && session.current_task_id ? state.tasks[session.current_task_id] : undefined;
    place(task ? (task.phase ?? null) : undefined, row);
  }
  if (state) {
    for (const slot of pickupSlots(state)) {
      place(slot.task ? (slot.task.phase ?? null) : undefined, batonRow(slot, nowMs));
    }
    // a phase with work in progress (open tasks started, or agents in it) is "in progress"
    for (const task of Object.values(state.tasks)) {
      const node = phases.get(phaseKey(task.phase));
      if (node && node.glyph === '○' && ['claimed', 'in_progress', 'blocked', 'review', 'stalled'].includes(task.status)) {
        node.glyph = '◉';
        node.status = 'in progress';
      }
    }
  }
  for (const node of phases.values()) {
    if (node.glyph !== '✓' && (node.children.length > 0 || node.done > 0)) {
      node.glyph = '◉';
      node.status = 'in progress';
    }
    node.children.sort((a, b) => Number(a.kind === 'baton') - Number(b.kind === 'baton'));
  }
  return { phases: [...phases.values()], loose, live: item.live, partial: state === null };
}

/** A progress rail drawn in mono blocks: `▰▰▰▱▱▱` (width cells). */
export function progressRail(done: number, total: number, width = 12): string {
  if (total <= 0) return '▱'.repeat(width);
  const filled = Math.round((Math.min(done, total) / total) * width);
  return '▰'.repeat(filled) + '▱'.repeat(width - filled);
}

/** Box-drawing prefix for a row: `├─ ` / `└─ ` and the `│  ` gutter of the parent. */
export function branch(isLast: boolean): string {
  return isLast ? '└─' : '├─';
}
