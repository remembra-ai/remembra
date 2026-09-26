// Crew Channel model (spec §5.7, §9.8): pure functions behind the channel
// screen, so the rules are tested without a browser.
//
// * threads: roots and replies grouped by `thread_root_id`, newest activity first;
// * the composer: `/decide …` (a human decision, in force at once), `/freeze
//   <zone> <reason>` (a human freeze), or a plain message of the chosen kind;
// * `@` autocomplete over what the server routes (callsign, agent, crew,
//   zone:<slug>, task:T-<n>), using the server's exact mention grammar;
// * the delivery expectation shown under the composer ("cc-1 will see this at
//   its next turn", "codex-1 (advisory) will see this at its next MCP call or
//   session start").
//
// Message bodies, task titles and zone titles are untrusted text: nothing here
// builds HTML from them; the screens render them as plain text.

import type {
  AuthorKind,
  CrewState,
  DecisionView,
  MessageKind,
  MessageView,
  SessionState,
  TaskView,
  ZoneView,
} from '../../../lib/crew/types';
import { LIVE_PRESENCE_STATES } from '../../../lib/crew/types';
import { parseServerTime } from '../../../lib/time';

/** A message as the channel shows it: the REST row (`message_api`) or a live event's MessageView. */
export interface ChannelMessage extends MessageView {
  crew_id?: string;
  author_user_id?: string | null;
  author_callsign?: string | null;
  author_label?: string | null;
  trust_score?: number | null;
  /** Server heuristic (§11.2): low trust score on agent text; shown collapsed with "show anyway". */
  collapsed?: boolean;
  created_at?: string | null;
  edited_at?: string | null;
}

export interface ChannelThread {
  /** The root id (the root message's id, or the `thread_root_id` of replies whose root is not loaded). */
  id: string;
  root: ChannelMessage | null;
  replies: ChannelMessage[];
  /** Highest seq in the thread: the list sorts by it. */
  lastSeq: number;
  /** A question with no answer yet. */
  openQuestion: boolean;
  /** Distinct author names in order of first appearance. */
  participants: string[];
}

/** Server limits (remembra.crew.schemas / channel.py). */
export const MAX_MESSAGE_BYTES = 8 * 1024;
export const MAX_MENTIONS = 20;
export const EDIT_WINDOW_MS = 10 * 60 * 1000;
/** Mentions of the human go to Needs-you; from a human they do nothing. */
export const HUMAN_ALIASES = new Set(['mani', 'human']);

/** Kinds a human picks in the composer (decision goes through `/decide`). */
export const HUMAN_KINDS = ['chat', 'note', 'question', 'answer'] as const;
export type HumanKind = (typeof HUMAN_KINDS)[number];

export const KIND_LABEL: Record<MessageKind, string> = {
  chat: 'chat',
  note: 'note',
  question: 'question',
  answer: 'answer',
  status: 'status',
  decision: 'decision',
  request_release: 'asks to release',
  system: 'system',
};

/** The server's client_msg_id grammar (channel.py CLIENT_MSG_ID_RE). */
export const CLIENT_MSG_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;

/** A fresh idempotency id for one draft: a retry of the same draft can never post twice. */
export function newClientMsgId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  return `m-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// ---------------------------------------------------------------------------
// Messages and threads
// ---------------------------------------------------------------------------

/** Union by id (the later list wins field by field), in seq order. */
export function mergeMessages(base: readonly ChannelMessage[], incoming: readonly ChannelMessage[]): ChannelMessage[] {
  const byId = new Map<string, ChannelMessage>();
  for (const m of base) byId.set(m.id, m);
  for (const m of incoming) {
    const prev = byId.get(m.id);
    byId.set(m.id, prev ? mergeOne(prev, m) : m);
  }
  return [...byId.values()].sort((a, b) => a.seq - b.seq || a.id.localeCompare(b.id));
}

function mergeOne(prev: ChannelMessage, next: ChannelMessage): ChannelMessage {
  const out: ChannelMessage = { ...prev };
  for (const [key, value] of Object.entries(next) as [keyof ChannelMessage, unknown][]) {
    if (value !== undefined) (out as unknown as Record<string, unknown>)[key] = value;
  }
  // A live event carries the body clipped to 4,000 characters: keep the full REST body
  // unless the message really changed (edited or redacted since).
  if (next.body_truncated && prev.body && !prev.body_truncated && next.edited === prev.edited && next.redacted === prev.redacted) {
    out.body = prev.body;
    out.body_truncated = false;
  }
  return out;
}

export function rootIdOf(message: Pick<ChannelMessage, 'id' | 'thread_root_id'>): string {
  return message.thread_root_id || message.id;
}

function isAnswered(root: ChannelMessage | null, replies: ChannelMessage[]): boolean {
  if (!root) return true;
  return replies.some((r) => r.kind === 'answer' || (r.author_kind !== root.author_kind || r.author_session_id !== root.author_session_id));
}

/** Threads, most recent activity first. */
export function groupThreads(messages: readonly ChannelMessage[], nameOf: (m: ChannelMessage) => string): ChannelThread[] {
  const threads = new Map<string, ChannelThread>();
  const sorted = [...messages].sort((a, b) => a.seq - b.seq);
  for (const m of sorted) {
    const id = rootIdOf(m);
    let t = threads.get(id);
    if (!t) {
      t = { id, root: null, replies: [], lastSeq: 0, openQuestion: false, participants: [] };
      threads.set(id, t);
    }
    if (m.id === id) t.root = m;
    else t.replies.push(m);
    t.lastSeq = Math.max(t.lastSeq, m.seq);
    const name = nameOf(m);
    if (!t.participants.includes(name)) t.participants.push(name);
  }
  for (const t of threads.values()) {
    t.openQuestion = t.root?.kind === 'question' && !t.root.redacted && !isAnswered(t.root, t.replies);
  }
  return [...threads.values()].sort((a, b) => b.lastSeq - a.lastSeq);
}

/** Who wrote a message, with the provenance label every agent line carries (§5.8, §9.8). */
export interface AuthorInfo {
  kind: AuthorKind;
  name: string;
  agentId: string | null;
  /** 'you' | 'human' | 'system' | 'key-verified' | 'self-declared' */
  provenance: string;
}

export function authorOf(message: ChannelMessage, state: CrewState | null, currentUserId: string | null): AuthorInfo {
  if (message.author_kind === 'human') {
    const you = !!currentUserId && message.author_user_id === currentUserId;
    return { kind: 'human', name: you ? 'you' : 'human', agentId: null, provenance: you ? 'you' : 'human' };
  }
  if (message.author_kind === 'system') return { kind: 'system', name: 'remembra', agentId: null, provenance: 'system' };
  const session = message.author_session_id ? state?.sessions[message.author_session_id] : undefined;
  const name = message.author_callsign || session?.callsign || message.author_agent_id || 'agent';
  return {
    kind: 'agent',
    name,
    agentId: message.author_agent_id ?? session?.agent_id ?? null,
    provenance: message.author_verified ? 'key-verified' : 'self-declared',
  };
}

/** Whether the signed-in human may still edit this message (own, ≤10 min, not redacted). */
export function canEdit(message: ChannelMessage, currentUserId: string | null, now: Date): boolean {
  if (message.author_kind !== 'human' || message.redacted || !currentUserId || message.author_user_id !== currentUserId) return false;
  const created = parseServerTime(message.created_at);
  return !!created && now.getTime() - created.getTime() <= EDIT_WINDOW_MS;
}

// ---------------------------------------------------------------------------
// Mentions (the server grammar, channel.py MENTION_RE and parse_mentions)
// ---------------------------------------------------------------------------

const MENTION_RE = /(?<![A-Za-z0-9_@/.\\-])@([A-Za-z0-9][A-Za-z0-9._:-]{0,79})/g;
const TOKEN_CHARS = /^[A-Za-z0-9._:-]*$/;
const BOUNDARY = /[A-Za-z0-9_@/.\\-]/;

/** `@tokens` in order, lowercased, deduplicated, trailing `.:-` dropped, at most 20 (as the server routes them). */
export function parseMentions(body: string): string[] {
  const out: string[] = [];
  for (const match of body.matchAll(MENTION_RE)) {
    const token = match[1].replace(/[.:-]+$/, '').toLowerCase();
    if (token && !out.includes(token)) out.push(token);
    if (out.length >= MAX_MENTIONS) break;
  }
  return out;
}

export interface MentionOption {
  /** What goes after `@`. */
  token: string;
  group: 'session' | 'agent' | 'crew' | 'zone' | 'task';
  /** Short label for the list (callsign, slug, T-n). */
  label: string;
  /** Plain-text hint (may contain a task or zone title: render as text). */
  hint: string;
}

export function isLiveSession(s: Pick<SessionState, 'state'>): boolean {
  return LIVE_PRESENCE_STATES.includes(s.state);
}

function liveSessionsOf(state: CrewState): SessionState[] {
  return Object.values(state.sessions)
    .filter(isLiveSession)
    .sort((a, b) => a.callsign.localeCompare(b.callsign, undefined, { numeric: true }));
}

/** Everything a mention can address in this crew, in the order the list shows it. */
export function mentionOptions(state: CrewState): MentionOption[] {
  const live = liveSessionsOf(state);
  const out: MentionOption[] = live.map((s) => ({
    token: s.callsign,
    group: 'session',
    label: s.callsign,
    hint: `${s.agent_id} (${s.agent_verified ? 'key-verified' : 'self-declared'}) · ${when(s)}`,
  }));
  const agents = [...new Set(live.map((s) => s.agent_id))].sort();
  for (const agent of agents) {
    const n = live.filter((s) => s.agent_id === agent && s.agent_verified).length;
    out.push({ token: agent, group: 'agent', label: agent, hint: `its key-verified live sessions (${n})` });
  }
  out.push({ token: 'crew', group: 'crew', label: 'crew', hint: `every live agent (${live.length}) and the Crew inbox` });
  const zones = Object.values(state.zones)
    .filter((z) => !z.builtin)
    .sort((a, b) => a.slug.localeCompare(b.slug));
  for (const z of zones) out.push({ token: `zone:${z.slug}`, group: 'zone', label: `zone:${z.slug}`, hint: `whoever holds ${z.title || z.slug}` });
  const tasks = Object.values(state.tasks)
    .filter((t) => !['done', 'cancelled'].includes(t.status))
    .sort((a, b) => b.number - a.number);
  for (const t of tasks) out.push({ token: `task:T-${t.number}`, group: 'task', label: `task:T-${t.number}`, hint: t.title });
  return out;
}

/** The `@…` being typed at the caret, or null. */
export function activeMention(text: string, caret: number): { start: number; query: string } | null {
  const upto = text.slice(0, caret);
  const at = upto.lastIndexOf('@');
  if (at < 0) return null;
  if (at > 0 && BOUNDARY.test(upto[at - 1])) return null;
  const query = upto.slice(at + 1);
  if (!TOKEN_CHARS.test(query) || query.length > 80) return null;
  return { start: at, query };
}

/** Options matching the typed query: prefix matches first, then substring matches. */
export function filterMentions(options: readonly MentionOption[], query: string, limit = 8): MentionOption[] {
  const q = query.toLowerCase();
  if (!q) return options.slice(0, limit);
  const prefix = options.filter((o) => o.token.toLowerCase().startsWith(q) || o.label.toLowerCase().startsWith(q));
  const rest = options.filter((o) => !prefix.includes(o) && o.token.toLowerCase().includes(q));
  return [...prefix, ...rest].slice(0, limit);
}

/** Replace the active mention with `@token ` and return the new text and caret. */
export function applyMention(text: string, caret: number, token: string): { text: string; caret: number } {
  const m = activeMention(text, caret);
  if (!m) return { text, caret };
  const before = text.slice(0, m.start);
  const after = text.slice(caret).replace(/^[A-Za-z0-9._:-]*/, '');
  const spaced = after.startsWith(' ');
  const insert = `@${token}${spaced ? '' : ' '}`;
  // The caret lands after the space that follows the mention, ready for the next word.
  return { text: before + insert + after, caret: before.length + insert.length + (spaced ? 1 : 0) };
}

// ---------------------------------------------------------------------------
// Slash commands
// ---------------------------------------------------------------------------

export const SLASH_COMMANDS = [
  { name: 'decide', usage: '/decide <the decision>', hint: 'In force now, and in every agent brief' },
  { name: 'freeze', usage: '/freeze <zone> <reason>', hint: 'Hold a zone yourself; agents are denied' },
] as const;

/** The `/command` being typed at the start of the draft (before the first space), or null. */
export function activeCommand(text: string, caret: number): string | null {
  if (!text.startsWith('/')) return null;
  const head = text.slice(0, caret);
  if (/\s/.test(head)) return null;
  return head.slice(1).toLowerCase();
}

export type ComposerIntent =
  | { type: 'empty' }
  | { type: 'message'; kind: HumanKind; body: string }
  | { type: 'decide'; body: string; title: string }
  | { type: 'freeze'; zone: ZoneView; reason: string }
  | { type: 'invalid'; error: string };

function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** What pressing send will do with this draft. */
export function parseComposer(text: string, zones: readonly ZoneView[], kind: HumanKind): ComposerIntent {
  const trimmed = text.trim();
  if (!trimmed) return { type: 'empty' };
  if (utf8Length(trimmed) > MAX_MESSAGE_BYTES) return { type: 'invalid', error: `Too long: messages are at most ${MAX_MESSAGE_BYTES / 1024} KB.` };
  if (!trimmed.startsWith('/')) return { type: 'message', kind, body: trimmed };
  const match = /^\/([A-Za-z-]+)(?:\s+([\s\S]*))?$/.exec(trimmed);
  const command = match?.[1]?.toLowerCase() ?? '';
  const rest = (match?.[2] ?? '').trim();
  if (command === 'decide') {
    if (!rest) return { type: 'invalid', error: 'Write the decision after /decide, e.g. /decide GCT rounds half-up per line.' };
    const title = rest.split('\n').map((l) => l.trim()).find(Boolean) ?? rest;
    return { type: 'decide', body: rest, title: title.length > 200 ? `${title.slice(0, 199)}…` : title };
  }
  if (command === 'freeze') {
    const [slugRaw, ...reasonParts] = rest.split(/\s+/);
    const slug = (slugRaw ?? '').replace(/^zone:/i, '').toLowerCase();
    const reason = reasonParts.join(' ').trim();
    const candidates = zones.filter((z) => !z.builtin);
    if (!slug) {
      const names = candidates.map((z) => z.slug).join(', ');
      return { type: 'invalid', error: names ? `Name the zone: /freeze <zone> <reason>. Zones: ${names}.` : 'This crew has no zones to freeze yet.' };
    }
    if (slug === 'crew-policy') return { type: 'invalid', error: 'crew-policy is always protected; it cannot be frozen or claimed.' };
    const zone = candidates.find((z) => z.slug.toLowerCase() === slug);
    if (!zone) {
      const names = candidates.map((z) => z.slug).join(', ');
      return { type: 'invalid', error: `No zone "${slug}" in this crew.${names ? ` Zones: ${names}.` : ''}` };
    }
    if (zone.frozen_by) return { type: 'invalid', error: `Zone ${zone.slug} is already frozen.` };
    if (!reason) return { type: 'invalid', error: `Add a reason: /freeze ${zone.slug} I'm editing this myself.` };
    return { type: 'freeze', zone, reason: reason.slice(0, 500) };
  }
  return { type: 'invalid', error: `Unknown command /${command || '…'}. Try /decide or /freeze.` };
}

// ---------------------------------------------------------------------------
// Delivery expectation (§9.8 composer)
// ---------------------------------------------------------------------------

/** When a live session sees a channel item addressed to it (D13: agent-facing items wait for a turn or an MCP call). */
export function when(session: Pick<SessionState, 'adapter_enforcement' | 'client_kind' | 'state'>): string {
  if (session.state === 'paused') return 'when it is resumed';
  if (session.state === 'quota_blocked') return 'only if it comes back (it stopped)';
  if (session.state === 'quiet') return 'when it is heard from again';
  const hooked = session.adapter_enforcement === 'enforced' && session.client_kind !== 'mcp';
  return hooked ? 'at its next turn' : 'at its next MCP call or session start';
}

function sessionLine(s: SessionState, selfDeclaredFor?: string): string {
  const advisory = s.adapter_enforcement === 'advisory' ? ' (advisory)' : '';
  const label = selfDeclaredFor ? ` (labelled "addressed to ${selfDeclaredFor}, you are self-declared")` : '';
  return `${s.callsign}${advisory} will see this ${when(s)}${label}.`;
}

function taskByRef(tasks: Record<string, TaskView>, ref: string): TaskView | undefined {
  const m = /^t-([1-9][0-9]{0,6})$/.exec(ref);
  if (!m) return undefined;
  const n = Number(m[1]);
  return Object.values(tasks).find((t) => t.number === n);
}

function zoneHolders(state: CrewState, zone: ZoneView): string[] {
  return Object.values(state.claims)
    .filter((c) => c.zone_id === zone.id && ['active', 'offered', 'reserved'].includes(c.state) && c.holder_session_id)
    .map((c) => c.holder_session_id as string);
}

/**
 * One plain sentence per recipient, in mention order: who will see this message and when.
 * Mirrors the server's routing (channel.py `_route`), including the self-declared labelling.
 */
export function deliveryLines(body: string, state: CrewState | null): string[] {
  const mentions = parseMentions(body);
  if (!mentions.length) return ['Nobody is mentioned, so no agent is interrupted. It stays in the channel for anyone who reads it.'];
  if (!state) return [];
  const lines: string[] = [];
  const seen = new Set<string>();
  const live = liveSessionsOf(state);
  const push = (s: SessionState | undefined, selfDeclaredFor?: string) => {
    if (!s || seen.has(s.id)) return;
    seen.add(s.id);
    lines.push(sessionLine(s, selfDeclaredFor));
  };
  for (const token of mentions) {
    if (HUMAN_ALIASES.has(token)) {
      lines.push(`@${token} is you: it only pages you when an agent writes it.`);
      continue;
    }
    if (token === 'crew') {
      live.forEach((s) => seen.add(s.id));
      lines.push(`Every live agent (${live.length}) sees this at its next turn or MCP call; it also lands in the Crew inbox.`);
      continue;
    }
    if (token.startsWith('zone:')) {
      const slug = token.slice(5);
      const zone = Object.values(state.zones).find((z) => z.slug.toLowerCase() === slug);
      if (!zone) {
        lines.push(`No zone "${slug}" in this crew: nobody is notified for it.`);
        continue;
      }
      const holders = zoneHolders(state, zone).map((id) => state.sessions[id]).filter((s): s is SessionState => !!s && isLiveSession(s));
      if (!holders.length) lines.push(`Nobody holds zone ${zone.slug} right now: nobody is notified for it.`);
      holders.forEach((s) => push(s));
      continue;
    }
    if (token.startsWith('task:')) {
      const task = taskByRef(state.tasks, token.slice(5));
      const owner = task?.owner_session_id ? state.sessions[task.owner_session_id] : undefined;
      if (!task) lines.push(`No task ${token.slice(5).toUpperCase()} in this crew.`);
      else if (!owner || !isLiveSession(owner)) lines.push(`T-${task.number} has no live owner: nobody is notified for it.`);
      else push(owner);
      continue;
    }
    const bySign = live.find((s) => s.callsign.toLowerCase() === token);
    if (bySign) {
      push(bySign);
      continue;
    }
    const agentSessions = live.filter((s) => s.agent_id.toLowerCase() === token);
    if (agentSessions.length) {
      agentSessions.forEach((s) => push(s, s.agent_verified ? undefined : token));
      continue;
    }
    const knownAgent = Object.values(state.sessions).some((s) => s.agent_id.toLowerCase() === token);
    if (knownAgent) lines.push(`No ${token} session is live: it goes to ${token}'s agent inbox and leads its next session brief.`);
    else lines.push(`@${token} matches no live agent, zone or task: nobody is notified for it.`);
  }
  return lines;
}

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

export function decisionRef(d: Pick<DecisionView, 'number'>): string {
  return `D-${d.number}`;
}

/** "proposed by cc-2 (key-verified)" / "by you" style line for a decision. */
export function decisionAuthor(
  d: DecisionView,
  state: CrewState | null,
  sessions: readonly Pick<SessionState, 'id' | 'callsign' | 'agent_id' | 'agent_verified'>[] = [],
): string {
  if (d.decided_by_kind === 'human') return 'a human';
  if (d.decided_by_kind === 'system') return 'remembra';
  const session = state?.sessions[d.decided_by] ?? sessions.find((s) => s.id === d.decided_by);
  if (session) return `${session.callsign} · ${session.agent_id} (${session.agent_verified ? 'key-verified' : 'self-declared'})`;
  return d.decided_by || 'an agent';
}

export function splitDecisions(decisions: Record<string, DecisionView>): { inForce: DecisionView[]; proposed: DecisionView[] } {
  const all = Object.values(decisions).sort((a, b) => b.number - a.number);
  return { inForce: all.filter((d) => d.state === 'in_force'), proposed: all.filter((d) => d.state === 'proposed') };
}
