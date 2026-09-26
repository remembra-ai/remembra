/**
 * Framer Motion animation presets for Remembra Dashboard
 * Premium, subtle, performant animations inspired by Linear & Vercel
 */

import { useSyncExternalStore } from 'react';
import type { Variants, Transition } from 'framer-motion';

// ─── Spring Presets ─────────────────────────────────────────────
export const spring = {
  snappy: { type: 'spring', stiffness: 500, damping: 30 } as Transition,
  smooth: { type: 'spring', stiffness: 300, damping: 25 } as Transition,
  gentle: { type: 'spring', stiffness: 200, damping: 20 } as Transition,
  bouncy: { type: 'spring', stiffness: 400, damping: 15 } as Transition,
};

// ─── Page Transition ────────────────────────────────────────────
export const pageTransition: Variants = {
  initial: {
    opacity: 0,
    y: 12,
    scale: 0.99,
    filter: 'blur(4px)',
  },
  animate: {
    opacity: 1,
    y: 0,
    scale: 1,
    filter: 'blur(0px)',
    transition: {
      duration: 0.3,
      ease: [0.22, 1, 0.36, 1], // easeOutQuint
    },
  },
  exit: {
    opacity: 0,
    y: -8,
    scale: 0.995,
    filter: 'blur(2px)',
    transition: {
      duration: 0.15,
      ease: [0.4, 0, 1, 1], // easeIn
    },
  },
};

// ─── Stagger Container ─────────────────────────────────────────
export const staggerContainer: Variants = {
  initial: {},
  animate: {
    transition: {
      staggerChildren: 0.06,
      delayChildren: 0.1,
    },
  },
};

// ─── Stagger Items ──────────────────────────────────────────────
export const staggerItem: Variants = {
  initial: { opacity: 0, y: 16 },
  animate: {
    opacity: 1,
    y: 0,
    transition: {
      duration: 0.4,
      ease: [0.22, 1, 0.36, 1],
    },
  },
};

// ─── Fade In ────────────────────────────────────────────────────
export const fadeIn: Variants = {
  initial: { opacity: 0 },
  animate: {
    opacity: 1,
    transition: { duration: 0.3, ease: 'easeOut' },
  },
  exit: {
    opacity: 0,
    transition: { duration: 0.15 },
  },
};

// ─── Scale In (for modals, cards) ───────────────────────────────
export const scaleIn: Variants = {
  initial: { opacity: 0, scale: 0.95 },
  animate: {
    opacity: 1,
    scale: 1,
    transition: {
      duration: 0.2,
      ease: [0.22, 1, 0.36, 1],
    },
  },
  exit: {
    opacity: 0,
    scale: 0.98,
    transition: { duration: 0.15 },
  },
};

// ─── Slide In (for sidebars, panels) ────────────────────────────
export const slideInLeft: Variants = {
  initial: { opacity: 0, x: -20 },
  animate: {
    opacity: 1,
    x: 0,
    transition: { duration: 0.3, ease: [0.22, 1, 0.36, 1] },
  },
  exit: {
    opacity: 0,
    x: -20,
    transition: { duration: 0.2 },
  },
};

export const slideInRight: Variants = {
  initial: { opacity: 0, x: 20 },
  animate: {
    opacity: 1,
    x: 0,
    transition: { duration: 0.3, ease: [0.22, 1, 0.36, 1] },
  },
  exit: {
    opacity: 0,
    x: 20,
    transition: { duration: 0.2 },
  },
};

// ─── Card Hover (interactive cards) ─────────────────────────────
export const cardHover = {
  rest: {
    y: 0,
    boxShadow: '0 0 0 rgba(255, 91, 20, 0)',
  },
  hover: {
    y: -2,
    boxShadow: '0 8px 30px rgba(255, 91, 20, 0.12)',
    transition: spring.snappy,
  },
  tap: {
    y: 0,
    scale: 0.995,
    transition: { duration: 0.1 },
  },
};

// ─── Pulse Glow (for active/live indicators) ────────────────────
export const pulseGlow: Variants = {
  animate: {
    boxShadow: [
      '0 0 0 0 rgba(255, 91, 20, 0.4)',
      '0 0 0 8px rgba(255, 91, 20, 0)',
    ],
    transition: {
      duration: 2,
      repeat: Infinity,
      ease: 'easeInOut',
    },
  },
};

// ─── Number Count Up ────────────────────────────────────────────
export const countUpTransition: Transition = {
  duration: 0.8,
  ease: [0.22, 1, 0.36, 1],
};

// ═══════════════════════════════════════════════════════════════
// Crew mode: the reduced set (spec §9.13 delight rules, §9.16 reduced motion)
//
// Crew screens use only the presets below. Every preset has a reduced-motion
// form: nothing travels, scales or blurs; state changes land instantly (or as
// a short opacity fade that carries no position). Delight moments (the baton
// pass in BatonTransit, the "Crew assembled" line in CrewAssembled) go through
// one shared DelightGate (lib/crew/delight.ts), which enforces the rules: one
// animation at a time, none while a needs-you item is open, dismissible, never
// longer than 600 ms.
// ═══════════════════════════════════════════════════════════════

/** The longest a crew animation may run (§9.13). */
export const CREW_MAX_MS = 600;

/** `prefers-reduced-motion: reduce`, read safely (false outside a browser). */
export function prefersReducedMotion(win: { matchMedia?: (q: string) => { matches: boolean } } | null = typeof window !== 'undefined' ? window : null): boolean {
  try {
    return !!win?.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  } catch {
    return false;
  }
}

/** Subscribe to the reduced-motion preference (for useSyncExternalStore). */
export function onReducedMotionChange(listener: () => void): () => void {
  if (typeof window === 'undefined' || !window.matchMedia) return () => {};
  const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
  mq.addEventListener?.('change', listener);
  return () => mq.removeEventListener?.('change', listener);
}

export interface CrewMotion {
  reduced: boolean;
  /** A feed row or card arriving at the top of a list. */
  rowEnter: Variants;
  /** A pill or chip appearing (new-events pill, live pill). */
  pill: Variants;
  /** The baton pass along its dashed bezier (§9.4): full length, or an instant move. */
  batonPass: Transition;
  /** A drawer or sheet sliding in (phone check-in sheets, zone drawer). */
  sheet: Variants;
  /** Duration of the orange pixel packets on the live strip; 0 = packets are not drawn. */
  packetMs: number;
  /** Whether ambient art (the dithered cloud) may drift; false = drawn once, still. */
  ambient: boolean;
}

const EASE_OUT: [number, number, number, number] = [0.22, 1, 0.36, 1];

/** The crew motion presets, in their full or reduced form. */
export function crewMotion(reduced: boolean): CrewMotion {
  if (reduced) {
    const still: Variants = { initial: { opacity: 1 }, animate: { opacity: 1, transition: { duration: 0 } }, exit: { opacity: 0, transition: { duration: 0 } } };
    return {
      reduced: true,
      rowEnter: still,
      pill: still,
      batonPass: { duration: 0 },
      sheet: still,
      packetMs: 0,
      ambient: false,
    };
  }
  return {
    reduced: false,
    rowEnter: {
      initial: { opacity: 0, y: -6 },
      animate: { opacity: 1, y: 0, transition: { duration: 0.22, ease: EASE_OUT } },
      exit: { opacity: 0, transition: { duration: 0.12 } },
    },
    pill: {
      initial: { opacity: 0, y: -4, scale: 0.96 },
      animate: { opacity: 1, y: 0, scale: 1, transition: { duration: 0.18, ease: EASE_OUT } },
      exit: { opacity: 0, y: -4, transition: { duration: 0.12 } },
    },
    batonPass: { duration: CREW_MAX_MS / 1000, ease: EASE_OUT },
    sheet: {
      initial: { opacity: 0, y: 24 },
      animate: { opacity: 1, y: 0, transition: { duration: 0.24, ease: EASE_OUT } },
      exit: { opacity: 0, y: 24, transition: { duration: 0.16 } },
    },
    packetMs: 900,
    ambient: true,
  };
}

export type DelightKind = 'baton_pass' | 'crew_assembled';

export interface DelightRequest {
  kind: DelightKind;
  /** Wanted duration; clamped to CREW_MAX_MS. */
  ms: number;
}

export interface DelightGrant {
  kind: DelightKind;
  /** Duration to animate for (0 = reduced motion: apply the end state at once). */
  ms: number;
  /** Stop early (the viewer dismissed it, or the screen went away). */
  dismiss: () => void;
  /** Still allowed to play: false once dismissed, over, or a needs-you item opened (jump to the end state). */
  active: () => boolean;
}

export type DelightRefusal = 'busy' | 'needs_you_open';

/**
 * The delight rules as a gate (§9.13): one animation at a time, none while a
 * needs-you item is open, ≤600 ms, dismissible, and the reduced-motion form
 * (ms = 0) when the viewer asked for it. Framework-free; the clock is injected.
 */
export class DelightGate {
  private current: { kind: DelightKind; until: number; token: number } | null = null;
  private token = 0;
  private needsYouOpen = false;
  private readonly now: () => number;
  private readonly reduced: () => boolean;
  private readonly listeners = new Set<() => void>();

  constructor(options: { now?: () => number; reduced?: () => boolean } = {}) {
    this.now = options.now ?? (() => Date.now());
    this.reduced = options.reduced ?? (() => prefersReducedMotion());
  }

  /** Called when a running delight is stopped from outside (a needs-you item opened). */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get needsYou(): boolean {
    return this.needsYouOpen;
  }

  /** Tell the gate whether a needs-you item is open on screen. Opening one stops any running delight. */
  setNeedsYouOpen(open: boolean): void {
    if (this.needsYouOpen === open) return;
    this.needsYouOpen = open;
    if (open && this.current) {
      this.current = null;
      for (const l of [...this.listeners]) l();
    }
  }

  get playing(): DelightKind | null {
    if (this.current && this.now() >= this.current.until) this.current = null;
    return this.current?.kind ?? null;
  }

  request(req: DelightRequest): DelightGrant | DelightRefusal {
    if (this.needsYouOpen) return 'needs_you_open';
    if (this.playing) return 'busy';
    const ms = this.reduced() ? 0 : Math.max(0, Math.min(CREW_MAX_MS, Math.round(req.ms)));
    const token = ++this.token;
    if (ms > 0) this.current = { kind: req.kind, until: this.now() + ms, token };
    return {
      kind: req.kind,
      ms,
      dismiss: () => {
        if (this.current?.token === token) this.current = null;
      },
      active: () => ms > 0 && this.current?.token === token && this.now() < this.current.until,
    };
  }
}

const FULL_MOTION = crewMotion(false);
const REDUCED_MOTION = crewMotion(true);

/** The crew presets for this viewer, following `prefers-reduced-motion` live. */
export function useCrewMotion(): CrewMotion {
  const reduced = useSyncExternalStore(onReducedMotionChange, () => prefersReducedMotion(), () => false);
  return reduced ? REDUCED_MOTION : FULL_MOTION;
}
