// The Policy panel's read model (spec §9.1 "Policy", §8.4, §8.5, D10, D34):
// enforcement level, git-hook status per checkout, bypass-code timing and the
// enforcement truth table. Pure functions over the live reducer state.

import { liveSessions } from '../../../lib/crew/selectors';
import type { CrewState, EnforcementLevel, GithookState, SessionState } from '../../../lib/crew/types';

export const ENFORCEMENT_RANK: Record<EnforcementLevel, number> = { off: 0, observe: 1, enforce: 2 };

export const ENFORCEMENT_CHOICES: { value: EnforcementLevel; label: string; meaning: string }[] = [
  {
    value: 'enforce',
    label: 'Enforce',
    meaning:
      'Held zones are denied to other agents before the write where their hooks enforce it, and at commit and push where the git gates are installed. The default.',
  },
  {
    value: 'observe',
    label: 'Observe',
    meaning:
      'Held zones warn instead of deny, and every would-be deny is logged. Paused agents, crew policy, protected or frozen zones, foreign checkouts and same-checkout clobbers stay denied.',
  },
  {
    value: 'off',
    label: 'Off',
    meaning: 'The gate steps aside entirely: nothing is denied, not even edits to crew policy. Presence, batons, tasks and reports keep working.',
  },
];

/** Lowering protection (enforce → observe/off, observe → off) is human-only and is announced to the crew (D10). */
export function isLowering(from: EnforcementLevel, to: EnforcementLevel): boolean {
  return ENFORCEMENT_RANK[to] < ENFORCEMENT_RANK[from];
}

// ---------------------------------------------------------------------------
// Git hooks per checkout (§8.4)
// ---------------------------------------------------------------------------

const HOOK_RANK: Record<GithookState, number> = { missing: 3, unknown: 2, chained: 1, ok: 0 };

export interface CheckoutRow {
  key: string;
  hostId: string | null;
  worktreeId: string | null;
  branches: string[];
  sessions: SessionState[];
  /** The worst state reported by any session in this checkout. */
  hook: GithookState;
}

/** Live sessions grouped by checkout (host + worktree); missing hooks first, then by first callsign. */
export function checkoutRows(state: CrewState): CheckoutRow[] {
  const groups = new Map<string, CheckoutRow>();
  for (const s of liveSessions(state)) {
    const key = s.worktree_id ? `${s.host_id ?? '?'}|${s.worktree_id}` : `session|${s.id}`;
    let row = groups.get(key);
    if (!row) {
      row = { key, hostId: s.host_id ?? null, worktreeId: s.worktree_id ?? null, branches: [], sessions: [], hook: 'ok' };
      groups.set(key, row);
    }
    row.sessions.push(s);
    if (s.branch && !row.branches.includes(s.branch)) row.branches.push(s.branch);
    const hook: GithookState = s.githook_state ?? 'unknown';
    if (HOOK_RANK[hook] > HOOK_RANK[row.hook]) row.hook = hook;
  }
  return [...groups.values()].sort((a, b) => HOOK_RANK[b.hook] - HOOK_RANK[a.hook] || a.sessions[0].callsign.localeCompare(b.sessions[0].callsign, undefined, { numeric: true }));
}

export const HOOK_TEXT: Record<GithookState, string> = {
  ok: 'commit gate ✓ · push gate ✓',
  chained: 'commit gate ✓ · push gate ✓ (chained through Husky/lefthook)',
  missing: 'commit gate: missing',
  unknown: 'not reported yet',
};

/** Re-install the git gates in that checkout (shows the diff, asks at the terminal). */
export const HOOK_FIX_COMMAND = 'remembra-crew connect --git-hooks --apply';

// ---------------------------------------------------------------------------
// Bypass codes (D34)
// ---------------------------------------------------------------------------

export const BYPASS_MINUTES = [5, 10, 15] as const;

/** `12:04 left`, or null once expired. `offsetMs` = server clock − client clock. */
export function codeTimeLeft(expiresAt: string, nowMs: number, offsetMs = 0): string | null {
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return null;
  const s = Math.floor((at - (nowMs + offsetMs)) / 1000);
  if (s <= 0) return null;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')} left`;
}

/** Where a human types the code (never shown to agents). */
export function bypassUsage(scope: string, code: string): string {
  if (scope === 'commit') return `REMEMBRA_BYPASS=${code} git commit`;
  if (scope === 'push') return `REMEMBRA_BYPASS=${code} git push`;
  return `REMEMBRA_BYPASS=${code}`;
}

// ---------------------------------------------------------------------------
// Enforcement truth table (§8.5), shown verbatim in the UI and the docs
// ---------------------------------------------------------------------------

export const TRUTH_TABLE: { agent: string; before: string; commit: string; push: string; after: string }[] = [
  { agent: 'Claude Code (hooks)', before: 'enforced: PreToolUse deny, incl. MCP writes and foreign checkouts', commit: 'enforced', push: 'enforced', after: 'detected' },
  { agent: 'Codex / Gemini / Qwen / Kimi (verified by round trip)', before: 'enforced where verified; shell only for Codex', commit: 'enforced', push: 'enforced', after: 'detected' },
  { agent: 'Same, unverified, own worktree', before: 'read-only fence (cooperative)', commit: 'enforced', push: 'enforced', after: 'detected' },
  { agent: 'Cursor', before: 'read-only fence', commit: 'enforced', push: 'enforced', after: 'detected' },
  { agent: 'MCP-only', before: 'advisory (crew_guard, refusals)', commit: 'enforced if hooks installed', push: 'enforced if hooks installed', after: 'detected from checkpoints and close' },
  { agent: 'Any agent with a human bypass code', before: 'allowed, recorded', commit: 'allowed, recorded', push: 'allowed, recorded', after: 'moment + audit' },
];

export const TRUST_FOOTNOTE =
  'Local enforcement coordinates cooperative agents. It cannot stop an agent deliberately working around it on your machine; every bypass is recorded. A server-side check on your git host is not part of this release.';

/** Which truth-table row describes a live session. */
export function truthRowFor(session: Pick<SessionState, 'agent_id' | 'adapter' | 'adapter_enforcement' | 'client_kind'>): number {
  const who = `${session.adapter ?? ''} ${session.agent_id}`.toLowerCase();
  if (session.client_kind === 'mcp') return 4;
  if (who.includes('claude')) return 0;
  if (who.includes('cursor')) return 3;
  return session.adapter_enforcement === 'enforced' ? 1 : 2;
}
