// The baton pass (spec §9.4 step 3, §9.13): when `baton.passed` arrives, an
// orange pixel packet runs a 600 ms dashed bezier from the lane (or pickup
// slot) that put the baton down to the lane that picked it up, with "work
// restored ✓" when the saved work was applied.
//
// Every pass asks the shared delight gate (lib/crew/delight.ts) first: one
// animation at a time, none while a needs-you item is open, ≤600 ms. Esc or a
// click anywhere on the track dismisses a running pass. When the gate refuses,
// the viewer prefers reduced motion or a dialog stays open, the pass takes the
// instant form: no travel, a short visual note instead. Screen readers hear the
// pass once, from the crew's moment announcer (CrewMomentAnnouncer), so this
// component has no live region of its own and raises no toast.

import { useEffect, useRef, useState, type RefObject } from 'react';
import { crewDelight } from '../../lib/crew/delight';
import type { DelightGate } from '../../lib/motion';
import type { BatonEntry } from '../../lib/crew/types';
import { batonText, bezierPath, cubicPoint, planPass, type Point } from './lane/transit';

const DURATION_MS = 600;
const HOLD_MS = 700;
const NOTE_MS = 2600;
const WAIT_MS = 3000;
const RETRY_MS = 150;

type Queued = BatonEntry & { waitedMs?: number };

function maxSeq(batons: readonly BatonEntry[]): number {
  return batons.reduce((m, b) => Math.max(m, b.seq), 0);
}

function centerOf(el: Element, box: DOMRect, side: 'from' | 'to'): Point {
  const r = el.getBoundingClientRect();
  // leave from the lane's right-hand track end, arrive at the lane's identity block
  return side === 'from'
    ? { x: r.left - box.left + Math.min(r.width - 24, r.width * 0.8), y: r.top - box.top + r.height / 2 }
    : { x: r.left - box.left + 24, y: r.top - box.top + Math.min(28, r.height / 2) };
}

export function BatonTransit({
  batons,
  containerRef,
  callsignOf,
  gate = crewDelight,
}: {
  batons: readonly BatonEntry[];
  containerRef: RefObject<HTMLElement | null>;
  callsignOf: (sessionId: string | null | undefined) => string;
  gate?: DelightGate;
}) {
  const [seen, setSeen] = useState(() => maxSeq(batons));
  const [queue, setQueue] = useState<Queued[]>([]);
  const [note, setNote] = useState('');
  const pathRef = useRef<SVGPathElement | null>(null);
  const packetRef = useRef<SVGGElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // new passes since this view opened (older ones are history, not news)
  const latest = maxSeq(batons);
  if (latest > seen) {
    setSeen(latest);
    setQueue((q) => [...q, ...batons.filter((b) => b.seq > seen).sort((a, b) => a.seq - b.seq)]);
  }

  // names are read when a pass plays; a new state (new callsigns) must not restart the animation
  const callsignRef = useRef(callsignOf);
  useEffect(() => {
    callsignRef.current = callsignOf;
  });

  const head = queue[0] ?? null;
  useEffect(() => {
    if (!head) return;
    const text = batonText(head, (sid) => callsignRef.current(sid));
    const container = containerRef.current;
    const svg = svgRef.current;
    const path = pathRef.current;
    const packet = packetRef.current;
    const from = head.from_session
      ? container?.querySelector(
          `[data-lane="${CSS.escape(String(head.from_session))}"], [data-slot-session="${CSS.escape(String(head.from_session))}"]`,
        )
      : null;
    const to = head.to_session ? container?.querySelector(`[data-lane="${CSS.escape(String(head.to_session))}"]`) : null;
    const dialogOpen = !!document.querySelector('[role="dialog"][aria-modal="true"]');
    let raf = 0;
    let timer = 0;
    const next = () => setQueue((q) => (q[0] === head ? q.slice(1) : q));
    const retry = () => {
      timer = window.setTimeout(() => {
        setQueue((q) => (q[0] === head ? [{ ...head, waitedMs: (head.waitedMs ?? 0) + RETRY_MS }, ...q.slice(1)] : q));
      }, RETRY_MS);
      return () => window.clearTimeout(timer);
    };
    const instant = () => {
      // the instant form: the lanes already show the new holder; a short visual note says what moved
      queueMicrotask(() => setNote(text));
      timer = window.setTimeout(() => {
        setNote('');
        next();
      }, NOTE_MS);
      return () => window.clearTimeout(timer);
    };
    const waitedMs = head.waitedMs ?? 0;
    const canDraw = !!(container && svg && path && packet && to);
    // A dialog is open (often the one that caused this pass): wait for it to close, then play.
    if (planPass({ dialogOpen, waitedMs, maxWaitMs: WAIT_MS, grant: null, canDraw }) === 'wait') return retry();
    const grant = gate.request({ kind: 'baton_pass', ms: DURATION_MS });
    const plan = planPass({ dialogOpen, waitedMs, maxWaitMs: WAIT_MS, grant, canDraw });
    if (plan === 'wait') return retry();
    if (plan === 'instant' || typeof grant === 'string' || !container || !svg || !path || !packet || !to) {
      if (typeof grant !== 'string') grant.dismiss();
      return instant();
    }
    const box = container.getBoundingClientRect();
    // the lane that put the baton down may be gone (lost): the pass then starts where pickup slots sit
    const a = from ? centerOf(from, box, 'from') : { x: box.width * 0.6, y: Math.max(40, container.scrollHeight - 48) };
    const b = centerOf(to, box, 'to');
    const curve = bezierPath(a, b, box.width);
    path.setAttribute('d', curve.d);
    svg.style.opacity = '1';
    const dismiss = () => grant.dismiss();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dismiss();
    };
    window.addEventListener('keydown', onKey);
    container.addEventListener('pointerdown', dismiss);
    const cleanupInput = () => {
      window.removeEventListener('keydown', onKey);
      container.removeEventListener('pointerdown', dismiss);
    };
    const finish = (held: boolean) => {
      cleanupInput();
      timer = window.setTimeout(
        () => {
          svg.style.opacity = '0';
          next();
        },
        held ? HOLD_MS : 0,
      );
    };
    const start = performance.now();
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / DURATION_MS);
      if (k < 1 && !grant.active()) {
        // dismissed, or a needs-you item opened: stop here, the lanes already show the end state
        finish(false);
        return;
      }
      const eased = 1 - Math.pow(1 - k, 3);
      const p = cubicPoint(curve, eased);
      // snap to a 3 px grid: the packet moves like a pixel, not a blur
      packet.setAttribute('transform', `translate(${Math.round(p.x / 3) * 3} ${Math.round(p.y / 3) * 3})`);
      path.style.strokeDashoffset = String((1 - eased) * 40);
      if (k < 1) raf = requestAnimationFrame(step);
      else {
        grant.dismiss();
        finish(true);
      }
    };
    raf = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(timer);
      cleanupInput();
      grant.dismiss();
    };
  }, [head, containerRef, gate]);

  return (
    <>
      <svg
        ref={svgRef}
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 z-20 h-full w-full overflow-visible transition-opacity duration-300"
        style={{ opacity: 0 }}
      >
        <path ref={pathRef} fill="none" stroke="var(--signal)" strokeWidth="2" strokeDasharray="6 6" strokeLinecap="square" />
        <g ref={packetRef}>
          <rect x="-4" y="-4" width="8" height="8" fill="var(--signal)" />
          <rect x="-12" y="-3" width="6" height="6" fill="var(--signal)" opacity="0.45" />
          <rect x="-19" y="-2" width="4" height="4" fill="var(--signal)" opacity="0.2" />
        </g>
      </svg>
      {note && (
        <p
          aria-hidden="true"
          data-baton-note=""
          className="pointer-events-none absolute right-2 top-2 z-30 max-w-[80%] truncate rounded-[2px] border border-signal bg-panel px-2.5 py-1.5 font-mono text-[11px] text-ink shadow-sm"
        >
          {note}
        </p>
      )}
    </>
  );
}
