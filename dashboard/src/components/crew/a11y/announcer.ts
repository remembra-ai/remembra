// Screen-reader announcements for crew screens (§9.16): exactly one polite
// and one assertive live region. Moments are announced as they happen;
// presence (the 5-second activity frames) is never announced. Bursts are
// coalesced so a busy crew does not flood the reader.

import { realTimers, type Timers } from '../../../lib/crew/socket';
import type { MomentEntry } from '../../../lib/crew/types';

export type Politeness = 'polite' | 'assertive';

export interface Announcement {
  text: string;
  level: Politeness;
}

/** Moment types that interrupt (safety: someone is breaking or bypassing the rules). */
const ASSERTIVE_TYPES = new Set(['guard.tamper_blocked', 'gate.tampered', 'guard.bypass_used', 'collision.detected']);

/** The announcements for moments newer than `afterSeq`, oldest first. */
export function momentAnnouncements(moments: readonly MomentEntry[], afterSeq: number): Announcement[] {
  return moments
    .filter((m) => m.seq > afterSeq)
    .sort((a, b) => a.seq - b.seq)
    .map((m) => ({ text: m.summary, level: ASSERTIVE_TYPES.has(m.type) ? 'assertive' : 'polite' }));
}

/** Join queued messages into one announcement ("3 updates. a. b. c."), at most `max` spelled out. */
export function coalesce(texts: readonly string[], max = 3): string {
  const clean = texts.map((t) => t.trim().replace(/[.\s]+$/, '')).filter(Boolean);
  if (clean.length <= 1) return clean[0] ?? '';
  const shown = clean.slice(-max);
  const more = clean.length - shown.length;
  return `${clean.length} updates. ${shown.join('. ')}.${more ? ` And ${more} more.` : ''}`;
}

export interface RegionText {
  polite: string;
  assertive: string;
}

/**
 * Collects announcements and publishes one text per region at most every
 * `windowMs`. Identical consecutive texts get a trailing zero-width space
 * toggled so screen readers repeat them.
 */
export class Announcer {
  private queues: Record<Politeness, string[]> = { polite: [], assertive: [] };
  private timers_: Record<Politeness, unknown> = { polite: null, assertive: null };
  private text: RegionText = { polite: '', assertive: '' };
  private readonly listeners = new Set<() => void>();
  private readonly timers: Timers;
  private readonly windowMs: Record<Politeness, number>;

  constructor(options: { timers?: Timers; politeMs?: number; assertiveMs?: number } = {}) {
    this.timers = options.timers ?? realTimers;
    this.windowMs = { polite: options.politeMs ?? 1500, assertive: options.assertiveMs ?? 300 };
  }

  getText = (): RegionText => this.text;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  announce(text: string, level: Politeness = 'polite'): void {
    const t = text.trim();
    if (!t) return;
    this.queues[level].push(t);
    if (this.timers_[level] !== null) return;
    this.timers_[level] = this.timers.setTimeout(() => this.flush(level), this.windowMs[level]);
  }

  private flush(level: Politeness): void {
    this.timers_[level] = null;
    const queued = this.queues[level];
    this.queues[level] = [];
    let next = coalesce(queued);
    if (!next) return;
    if (next === this.text[level].replace(/\u200b$/, '')) next = this.text[level].endsWith('\u200b') ? next : `${next}\u200b`;
    this.text = { ...this.text, [level]: next };
    for (const l of [...this.listeners]) l();
  }

  /** Drop everything queued and clear both regions (sign-out, tests). */
  reset(): void {
    for (const level of ['polite', 'assertive'] as Politeness[]) {
      if (this.timers_[level] !== null) this.timers.clearTimeout(this.timers_[level]);
      this.timers_[level] = null;
      this.queues[level] = [];
    }
    this.text = { polite: '', assertive: '' };
    for (const l of [...this.listeners]) l();
  }
}

/** The dashboard's one announcer (the regions are rendered once, by CrewLiveRegions). */
export const crewAnnouncer = new Announcer();
