// The one delight gate of the crew screens (spec §9.13): the baton pass and the "Crew assembled"
// start line both ask it before they animate, so the rules hold across components: one animation
// at a time, none while a needs-you item is open, dismissible, ≤600 ms, reduced-motion form.
//
// "A needs-you item is open" is fed from two places: Mission Control while its crew has open
// needs-you items (the rail shows them), and any needs-you card whose action panel the viewer has
// expanded. Each holder keeps the gate closed while it is mounted and open; the gate reopens when
// the last holder lets go.

import { useEffect } from 'react';
import { DelightGate, type DelightKind, type DelightGrant, type DelightRefusal } from '../motion';

export const crewDelight = new DelightGate();

const holds = new WeakMap<DelightGate, number>();

/** Keep delight moments off until the returned release is called (a needs-you item is open on this screen). */
export function holdNeedsYou(gate: DelightGate = crewDelight): () => void {
  holds.set(gate, (holds.get(gate) ?? 0) + 1);
  gate.setNeedsYouOpen(true);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const left = Math.max(0, (holds.get(gate) ?? 1) - 1);
    holds.set(gate, left);
    if (left === 0) gate.setNeedsYouOpen(false);
  };
}

/** React binding of {@link holdNeedsYou}: holds the gate closed while `open` is true and the component is mounted. */
export function useNeedsYouOpen(open: boolean): void {
  useEffect(() => (open ? holdNeedsYou() : undefined), [open]);
}

/** Ask the shared gate for one delight moment. */
export function requestDelight(kind: DelightKind, ms: number, gate: DelightGate = crewDelight): DelightGrant | DelightRefusal {
  return gate.request({ kind, ms });
}
