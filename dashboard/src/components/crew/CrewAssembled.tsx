// "Crew assembled" (spec §9.13 L0 delight, §9.15 Animation): when a second agent joins and the
// crew goes from solo to multi (`crew.mode_changed` to `multi`) while the track is open, a dashed
// start line draws across the lanes (≤600 ms) with "Crew assembled · n agents", holds a moment
// and fades. It asks the shared delight gate first: refused (another animation, a needs-you item
// open) it stays quiet, the header's status strip already reads "Crew assembled ·"; reduced motion
// gets the line at once, without the draw. Esc or a click dismisses it. Screen readers get the
// moment from the crew's announcer, so the line is aria-hidden.

import { useEffect, useRef, useState, type RefObject } from 'react';
import { crewDelight } from '../../lib/crew/delight';
import type { DelightGate } from '../../lib/motion';
import type { CrewEvent } from '../../lib/crew/types';
import { assembledEvent } from './lane/transit';

const DRAW_MS = 600;
const HOLD_MS = 1600;
const FADE_MS = 300;

export function CrewAssembled({
  events,
  sinceSeq,
  agents,
  containerRef,
  gate = crewDelight,
}: {
  events: readonly CrewEvent[];
  /** The crew's seq when the track opened: older moves are history, not news. */
  sinceSeq: number;
  agents: number;
  containerRef: RefObject<HTMLElement | null>;
  gate?: DelightGate;
}) {
  const [baseline] = useState(sinceSeq);
  const [label, setLabel] = useState<string | null>(null);
  const lineRef = useRef<HTMLDivElement | null>(null);
  const grantMs = useRef(0);
  const agentsRef = useRef(agents);
  useEffect(() => {
    agentsRef.current = agents;
  });

  // the newest assembly since the track opened; a later one (solo again, then multi) plays again
  const seq = assembledEvent(events, baseline)?.seq ?? null;
  useEffect(() => {
    if (seq === null) return;
    const grant = gate.request({ kind: 'crew_assembled', ms: DRAW_MS });
    if (typeof grant === 'string') return; // refused: the status strip already says it
    grantMs.current = grant.ms;
    const n = agentsRef.current;
    queueMicrotask(() => setLabel(`Crew assembled · ${n} agent${n === 1 ? '' : 's'}`));
    const timers: number[] = [];
    const container = containerRef.current;
    const stop = () => {
      grant.dismiss();
      const line = lineRef.current;
      if (line) line.style.opacity = '0';
      timers.push(window.setTimeout(() => setLabel(null), FADE_MS));
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') stop();
    };
    window.addEventListener('keydown', onKey);
    container?.addEventListener('pointerdown', stop);
    timers.push(window.setTimeout(stop, grant.ms + HOLD_MS));
    return () => {
      for (const t of timers) window.clearTimeout(t);
      window.removeEventListener('keydown', onKey);
      container?.removeEventListener('pointerdown', stop);
      grant.dismiss();
    };
  }, [seq, gate, containerRef]);

  // once the line is on screen: draw it (or, in the reduced form, show it at once)
  useEffect(() => {
    const line = lineRef.current;
    if (!label || !line) return;
    const raf = requestAnimationFrame(() => {
      const ms = grantMs.current;
      line.style.transition = ms > 0 ? `transform ${ms}ms cubic-bezier(0.22, 1, 0.36, 1), opacity ${FADE_MS}ms linear` : `opacity ${FADE_MS}ms linear`;
      line.style.opacity = '1';
      line.style.transform = 'scaleX(1)';
    });
    return () => cancelAnimationFrame(raf);
  }, [label]);

  if (!label) return null;
  return (
    <div
      ref={lineRef}
      aria-hidden="true"
      data-crew-assembled=""
      className="pointer-events-none absolute inset-x-0 top-0 z-20 origin-left"
      style={{ opacity: 0, transform: 'scaleX(0)' }}
    >
      <div className="border-t-2 border-dashed border-signal" />
      <p className="mt-1 inline-block rounded-[2px] bg-panel px-2 py-0.5 font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-signal-ink">
        {label}
      </p>
    </div>
  );
}
