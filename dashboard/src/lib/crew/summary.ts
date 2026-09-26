// Event summaries as people read them (§9 "server-template text"). The server writes each event's
// summary from ids, slugs and callsigns only; the hash-chained log keeps it as written. On screen the
// raw ids (`msg_01M3…`, `clm_01M3…`, `inb_…`, `tsk_…`) mean nothing to a person, so the feed, the
// Track ticker and the Latest moves panel run every summary through here: an id becomes what it
// names (T-14, a callsign, D-3, "the pos claim"), ids in brackets are dropped, snake_case words
// become words, and "1 sessions" becomes "1 session". Pure; unknown ids fall back to a plain noun,
// never the id. The text stays plain (rendered as text, never HTML).

import type { CrewState, EventRefs } from './types';

type Lookup = Pick<CrewState, 'tasks' | 'sessions' | 'claims' | 'zones' | 'decisions'> | null;

interface EventLike {
  summary?: string | null;
  refs?: EventRefs | null;
}

const ID = /\b(tsk|cs|clm|msg|inb|dec|rpt|zch|off|bat|col|hst|evt|mem|ckp)_[0-9A-Za-z]{4,}\b/g;
const NOUN: Record<string, string> = {
  tsk: 'a task',
  cs: 'a session',
  clm: 'a claim',
  msg: 'a message',
  inb: 'an item',
  dec: 'a decision',
  rpt: 'the report',
  zch: 'the change',
  off: 'an offer',
  bat: 'a baton',
  col: 'a collision',
  hst: 'a host',
  evt: 'an event',
  mem: 'a memory',
  ckp: 'a checkpoint',
};
// Server kinds and reasons written in summaries (crew/schemas.py). Only these become words: a zone slug
// or an agent id may contain an underscore and must stay as written.
const KIND_WORDS = new Set([
  'append_only', 'authentication_failed', 'baton_available', 'baton_reserved', 'baton_restore_failed',
  'baton_waiting', 'billing_error', 'bypass_used', 'claim_granted', 'collision_escalated', 'collision_notice',
  'collision_open', 'crew_files_removed', 'crewd_kill', 'decision_to_confirm', 'ended_dirty', 'exclusive_breach',
  'false_deny_alarm', 'first_write', 'foreign_checkout_write', 'githook_missing', 'handover_offer', 'host_lost',
  'host_unreachable', 'human_assign', 'human_hold', 'human_question', 'idle_park', 'invalid_request',
  'lease_expired', 'max_output_tokens', 'mcp_silent', 'merge_conflict_risk', 'micro_lease', 'model_not_found',
  'oauth_org_not_allowed', 'override_notice', 'process_exited', 'rate_limit', 'report_invariant', 'request_release',
  'reserved_for', 'review_report', 'same_checkout', 'same_file', 'same_worktree_file', 'same_zone_shared',
  'server_error', 'stale_epoch_write', 'stuck_agent', 'tamper_blocked', 'task_blocked', 'task_ready',
  'unattributed_change', 'usage_limit', 'zone_change_pending', 'zone_contested', 'zone_hoarding', 'parent_ended',
]);
const PLURAL_ONE = /\b1 (sessions|files|claims|tasks|items|agents|zones|messages|decisions|questions|batons|reports)\b/g;

function zoneSlug(state: Lookup, zoneId: unknown): string | null {
  if (!state || typeof zoneId !== 'string') return null;
  return state.zones[zoneId]?.slug ?? null;
}

function claimWhat(state: Lookup, claimId: string, refs: EventRefs): string | null {
  const claim = state?.claims[claimId];
  const slug = zoneSlug(state, claim?.zone_id ?? refs.zone_id);
  if (slug) return slug;
  return claim?.resource ?? null;
}

function taskLabel(state: Lookup, taskId: string): string | null {
  const task = state?.tasks[taskId];
  return task ? `T-${task.number}` : null;
}

/** A summary with ids replaced by what they name (see the file comment). */
export function humanSummary(state: Lookup, event: EventLike): string {
  let text = String(event.summary ?? '');
  if (!text) return text;
  const refs: EventRefs = event.refs ?? {};
  // "(msg_…)", "(inb_…)": the bracketed id adds nothing a person can use
  text = text.replace(/\s*\(\s*(?:msg|inb|evt|off|mem|ckp)_[0-9A-Za-z]{4,}\s*\)/g, '');
  // "baton clm_…" → "baton for T-14" (or "for pos"); "claim clm_…" → "pos claim"
  text = text.replace(/\bbaton (clm_[0-9A-Za-z]{4,})\b/g, (_m, id: string) => {
    const claim = state?.claims[id];
    const task = claim?.task_id ? taskLabel(state, claim.task_id) : typeof refs.task_id === 'string' ? taskLabel(state, refs.task_id) : null;
    const what = task ?? claimWhat(state, id, refs);
    return what ? `baton for ${what}` : 'a baton';
  });
  text = text.replace(/\b(reserved claim|claim|micro-lease) (clm_[0-9A-Za-z]{4,})\b/g, (_m, noun: string, id: string) => {
    const what = claimWhat(state, id, refs);
    return what ? `${noun} on ${what}` : noun;
  });
  // "inbox item inb_… claimed", "report rpt_… for T-1", "zone change zch_… approved": the noun is enough
  text = text.replace(/\b(item|report|change) (?:inb|rpt|zch)_[0-9A-Za-z]{4,}\b/g, '$1');
  text = text.replace(ID, (id: string, kind: string) => {
    if (kind === 'tsk') return taskLabel(state, id) ?? NOUN.tsk;
    if (kind === 'cs') return state?.sessions[id]?.callsign ?? NOUN.cs;
    if (kind === 'dec') {
      const d = state?.decisions[id];
      return d ? `D-${d.number}` : NOUN.dec;
    }
    if (kind === 'clm') {
      const what = claimWhat(state, id, refs);
      return what ? `the ${what} claim` : NOUN.clm;
    }
    return NOUN[kind] ?? 'an item';
  });
  // snake_case kinds and reasons read as words ("decision_to_confirm", "billing_error")
  text = text.replace(/\b[a-z]+(?:_[a-z]+)+\b/g, (w) => (KIND_WORDS.has(w) ? w.replace(/_/g, ' ') : w));
  text = text.replace(PLURAL_ONE, (_m, word: string) => `1 ${word.slice(0, -1)}`);
  return text.replace(/\s{2,}/g, ' ').trim();
}
