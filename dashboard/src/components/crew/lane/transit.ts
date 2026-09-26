// Geometry and wording for the baton pass (BatonTransit). Pure functions.

import type { BatonEntry, CrewEvent } from '../../../lib/crew/types';

export interface Point {
  x: number;
  y: number;
}

export interface Curve {
  p0: Point;
  p1: Point;
  p2: Point;
  p3: Point;
  d: string;
}

/**
 * A cubic bezier from a to b that swings out to the right and lands on b from
 * the side, so a pass between stacked lanes reads as a hand-off arc. Control
 * points stay inside [0, width] so the arc never leaves the lanes column.
 */
export function bezierPath(a: Point, b: Point, width = Number.POSITIVE_INFINITY): Curve {
  const dy = b.y - a.y;
  const bow = Math.max(60, Math.abs(dy) * 0.6);
  const clampX = (x: number) => Math.max(8, Math.min(width - 8, x));
  const p1 = { x: clampX(a.x + bow), y: a.y + dy * 0.15 };
  const p2 = { x: clampX(b.x + bow * 0.75), y: b.y - dy * 0.25 };
  const r = (n: number) => Math.round(n * 10) / 10;
  return { p0: a, p1, p2, p3: b, d: `M${r(a.x)} ${r(a.y)} C${r(p1.x)} ${r(p1.y)} ${r(p2.x)} ${r(p2.y)} ${r(b.x)} ${r(b.y)}` };
}

export function cubicPoint(c: Curve, t: number): Point {
  const u = 1 - t;
  const w0 = u * u * u;
  const w1 = 3 * u * u * t;
  const w2 = 3 * u * t * t;
  const w3 = t * t * t;
  return {
    x: w0 * c.p0.x + w1 * c.p1.x + w2 * c.p2.x + w3 * c.p3.x,
    y: w0 * c.p0.y + w1 * c.p1.y + w2 * c.p2.y + w3 * c.p3.y,
  };
}

const KIND_TEXT: Record<string, string> = {
  adopt: 'adopted',
  handover: 'handed over',
  same_checkout: 'picked up in the same checkout',
  human_assign: 'handed over by you',
  reserved_for: 'picked up (reserved for it)',
  first_write: 'picked up on first write',
};

/** "Baton passed cc-1 → cc-3 (adopted) · work restored ✓" */
export function batonText(baton: BatonEntry, callsignOf: (sessionId: string | null | undefined) => string): string {
  const from = baton.from_session ? callsignOf(baton.from_session as string) : 'a reserved slot';
  const to = callsignOf(baton.to_session as string);
  const kind = typeof baton.kind === 'string' ? (KIND_TEXT[baton.kind] ?? baton.kind) : null;
  let text = `Baton passed ${from} → ${to}${kind ? ` (${kind})` : ''}`;
  if (baton.restored === true) text += ' · work restored ✓';
  else if (baton.restored === false && baton.baton_ref) text += ' · saved work not restored yet';
  return text;
}

/** The newest `crew.mode_changed` → multi after `afterSeq` (the "Crew assembled" moment, §9.13), or null. */
export function assembledEvent(events: readonly CrewEvent[], afterSeq: number): CrewEvent | null {
  let found: CrewEvent | null = null;
  for (const e of events) {
    if (e.seq > afterSeq && e.type === 'crew.mode_changed' && e.payload?.to === 'multi' && (!found || e.seq > found.seq)) found = e;
  }
  return found;
}

/**
 * How one baton pass plays (§9.13), given what the delight gate said:
 * `wait` (retry shortly: a dialog is open, or another animation is running, up to `maxWaitMs`),
 * `instant` (no travel: refused while a needs-you item is open, reduced motion, nothing to draw
 * between, or waited too long), `animate` (the 600 ms bezier).
 */
export type PassPlan = 'wait' | 'instant' | 'animate';

export function planPass(input: {
  dialogOpen: boolean;
  waitedMs: number;
  maxWaitMs: number;
  /** The gate's answer; null when it was not asked (a dialog is open and we still wait). */
  grant: { ms: number } | 'busy' | 'needs_you_open' | null;
  canDraw: boolean;
}): PassPlan {
  const patient = input.waitedMs < input.maxWaitMs;
  if (input.dialogOpen && patient) return 'wait';
  if (input.grant === 'busy') return patient ? 'wait' : 'instant';
  if (input.grant === null || input.grant === 'needs_you_open') return 'instant';
  if (input.grant.ms === 0 || !input.canDraw || input.dialogOpen) return 'instant';
  return 'animate';
}
