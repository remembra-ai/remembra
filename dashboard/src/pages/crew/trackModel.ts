// Small pure helpers for the Mission Control header and status strip.

import { eventSessionId } from '../../lib/crew/reducer';
import { liveSessions } from '../../lib/crew/selectors';
import type { CrewEvent, CrewState } from '../../lib/crew/types';
import { parseServerTime } from '../../lib/time';
import { shortAge } from '../../components/crew/lane/model';

/** `branch@head` of the most recently active live session ("main@abc1234"), or null. */
export function trackBranch(state: CrewState): string | null {
  const sessions = liveSessions(state)
    .filter((s) => s.branch)
    .sort((a, b) => (parseServerTime(b.last_activity_at)?.getTime() ?? 0) - (parseServerTime(a.last_activity_at)?.getTime() ?? 0));
  const s = sessions[0];
  if (!s?.branch) return null;
  return s.head_commit ? `${s.branch}@${s.head_commit.slice(0, 7)}` : s.branch;
}

export interface LiveStatus {
  /** A short bold lead for special moves ("Crew assembled", "Baton passed"). */
  lead: string | null;
  text: string;
  age: string | null;
  /** The last move is under a minute old (the dot glows). */
  fresh: boolean;
}

const LEADS: Record<string, string> = {
  'baton.passed': 'Baton passed ·',
  'session.quota_blocked': 'Auto-handoff ·',
  'session.lost': 'Went silent ·',
  'guard.tamper_blocked': 'Tamper blocked ·',
  'guard.bypass_used': 'Bypass used ·',
  'task.done': 'Done ·',
  'collision.detected': 'Collision ·',
};

/** The live status strip: the crew's latest move, server-template summary (ids, slugs and callsigns only). */
export function liveStatus(state: CrewState, latest: CrewEvent | null, nowMs: number): LiveStatus {
  if (!latest) {
    const live = liveSessions(state).length;
    return { lead: null, text: live ? `${live} live · no moves in the last hour` : 'no agents running', age: null, fresh: false };
  }
  const at = parseServerTime(latest.ts);
  const ageS = at ? Math.max(0, (nowMs - at.getTime()) / 1000) : null;
  let lead = LEADS[latest.type] ?? null;
  if (latest.type === 'crew.mode_changed' && latest.payload?.to === 'multi') lead = 'Crew assembled ·';
  return {
    lead,
    text: latest.summary,
    age: ageS === null ? null : ageS < 5 ? 'just now' : `${shortAge(ageS)} ago`,
    fresh: ageS !== null && ageS < 60,
  };
}

/** The source ("reported" by StopFailure, "detected" by the transcript detector) of each session's last quota stop. */
export function quotaSources(events: readonly CrewEvent[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const e of events) {
    if (e.type !== 'session.quota_blocked') continue;
    const sid = eventSessionId(e);
    const source = e.payload?.source;
    if (sid && typeof source === 'string') out.set(sid, source);
  }
  return out;
}
