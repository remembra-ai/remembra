// Marshal's "why?" check for a waiting row on the Home setup checklist.
//
// Rules only: a fixed table picks one verdict from data the dashboard
// already reads (your keys, the trail and who picked each handoff up). No
// model is called and nothing is written. Every verdict says whether it is
// proven by that data ([!!]) or inferred from it ([??]); inferences that rest
// on key use carry the one-key caveat, because one key can serve every agent
// on a machine. tests/fixtures/marshal/diagnosis_cases.json holds the cases
// (the server port in M2 runs the same file).

import { api, type ApiKeyInfo } from './api';
import { relay, type AgentActivity, type TrailItem } from './relay';
import {
  WORKING_WINDOW_MINUTES,
  agentConnectCommand,
  agentMeta,
  askAgentDoctor,
  canonicalAgentId,
  doctorCommand,
  oneLineInstall,
  pipxRunDoctorCommand,
} from './agents';
import { minutesSince, parseServerTime, relativeTime } from './time';

/** How many trail entries the slip reads for pickups (the API's page maximum). */
export const SLIP_TRAIL_LIMIT = 100;
/** How many of the agent's own entries it reads. */
export const SLIP_AGENT_LIMIT = 5;

export const CODEX_TRUST_LINE = 'Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust.';
export const CODEX_TRUST_CLI = 'In the Codex CLI: run /hooks and trust SessionStart, UserPromptSubmit and SessionEnd.';
export const KEY_CAVEAT =
  "One key can serve every agent on a machine, so Remembra can't tell which agent used it. Doctor on that machine can.";
export const SLIP_FOOTER = 'Built by rules from your keys and trail. No model wrote this.';
export const RELAY_GUIDE = 'https://docs.remembra.dev/guides/relay/';
export const DETACHED_CLOSE_LOG = '~/.remembra/relay/last-detached-close.log';

export type VerdictCode =
  | 'NO_KEY'
  | 'KEY_NEVER_USED'
  | 'PICKS_UP_NEVER_CLOSES'
  | 'CODEX_TRUST'
  | 'NOTHING_WAITING'
  | 'HOOKS_NOT_FIRING'
  | 'STALE_CHECKPOINT'
  | 'HANDED_OFF';

/** The fields of a key the check reads (never the key itself: the list endpoint has only a preview). */
export type KeyEvidence = Pick<ApiKeyInfo, 'name' | 'created_at' | 'last_used_at' | 'active'>;

/** One read Marshal made, as a `›` line: what it read and what it found. */
export interface ReadLine {
  label: 'keys' | 'entries' | 'pickups' | 'trail';
  text: string;
  failed?: boolean;
}

/** A line to copy: `$` runs in a terminal, `>` is typed into an agent. */
export interface SlipCommand {
  prompt: '$' | '>';
  text: string;
  /** Screen-reader label for the copy group. */
  label: string;
  /** Shown before the line ("ask your agent:", "no upgrade yet?"). */
  caption?: string;
}

export interface Verdict {
  code: VerdictCode;
  /** true: the data shows it ([!!]); false: inferred from it ([??]). */
  proven: boolean;
  lines: ReadLine[];
  verdict: string;
  /** One sentence on why, when the verdict alone does not say. */
  detail: string | null;
  /** "Likely one of:" (inferred verdicts). */
  causes: string[];
  /** The honest line for an adapter that has never been run against its tool. */
  unverified: string | null;
  /** The one fix, or null when there is nothing to fix. */
  fix: { text: string; commands: SlipCommand[] } | null;
  then: string | null;
  /** Doctor on the agent's machine: the check that confirms (or finds) the cause. */
  check: { lead: string; commands: SlipCommand[] } | null;
  /** Every copyable line, in the order the slip shows them. */
  commands: string[];
  caveat: string | null;
  doc: string;
}

export interface DiagnosisInput {
  agentId: string;
  /** The Home summary's row for the agent (absent while the row is waiting). */
  summaryAgent?: AgentActivity | null;
  keys: KeyEvidence[];
  /** The newest entries across agents (SLIP_TRAIL_LIMIT). */
  trail: TrailItem[];
  /** The agent's own newest entries (SLIP_AGENT_LIMIT). */
  agentTrail: TrailItem[];
  now: Date;
  /** The server the one-line install saves the key for. */
  serverUrl?: string;
}

function plural(n: number, word: string, many = `${word}s`): string {
  return `${n} ${n === 1 ? word : many}`;
}

function isAgent(id: string, agentId: string | null | undefined): boolean {
  return canonicalAgentId(agentId) === id;
}

/** Key names are the user's own labels; the slip shows at most 40 characters of one. */
function keyLabel(name: string | null): string {
  const clean = (name || '').replace(/\s+/g, ' ').trim();
  if (!clean) return '';
  return clean.length > 40 ? `${clean.slice(0, 39)}…` : clean;
}

function newestFirst<T>(items: T[], at: (item: T) => string | null | undefined): T[] {
  return [...items].sort((a, b) => (parseServerTime(at(b))?.getTime() ?? 0) - (parseServerTime(at(a))?.getTime() ?? 0));
}

/** Handoffs in `trail` that `id` picked up (one per handoff, whatever the reader session). */
export function pickupsBy(id: string, trail: TrailItem[]): number {
  return trail.filter((item) => (item.picked_up_by ?? []).some((p) => isAgent(id, p.agent_id))).length;
}

export function keysLine(keys: KeyEvidence[], now: Date): ReadLine {
  const active = keys.filter((k) => k.active !== false);
  if (active.length === 0) return { label: 'keys', text: 'none active' };
  const used = newestFirst(
    active.filter((k) => k.last_used_at),
    (k) => k.last_used_at,
  );
  if (used.length === 0) return { label: 'keys', text: `${active.length} active · never used` };
  const label = keyLabel(used[0].name);
  return {
    label: 'keys',
    text: `${active.length} active · newest used ${relativeTime(used[0].last_used_at, now)}${label ? ` ("${label}")` : ''}`,
  };
}

export function entriesLine(agentId: string, agentTrail: TrailItem[], summaryAgent: AgentActivity | null | undefined, now: Date): ReadLine {
  const id = canonicalAgentId(agentId);
  const adapter = agentMeta(id).adapter ?? id;
  if (summaryAgent && summaryAgent.handoffs + summaryAgent.checkpoints > 0) {
    return {
      label: 'entries',
      text: `${adapter}: ${plural(summaryAgent.handoffs, 'handoff')} · ${plural(summaryAgent.checkpoints, 'checkpoint')} · newest ${relativeTime(summaryAgent.last_active, now)}`,
    };
  }
  const own = agentTrail.filter((item) => isAgent(id, item.agent_id));
  if (own.length === 0) return { label: 'entries', text: `${adapter}: no handoffs or checkpoints yet` };
  const handoffs = own.filter((item) => item.memory_type === 'handoff').length;
  const newest = newestFirst(own, (item) => item.created_at)[0];
  return {
    label: 'entries',
    text: `${adapter}: ${plural(handoffs, 'handoff')} · ${plural(own.length - handoffs, 'checkpoint')} in its last ${own.length} · newest ${relativeTime(newest.created_at, now)}`,
  };
}

export function pickupsLine(agentId: string, trail: TrailItem[]): ReadLine {
  const id = canonicalAgentId(agentId);
  const adapter = agentMeta(id).adapter ?? id;
  const others = trail.filter((item) => item.memory_type === 'handoff' && !isAgent(id, item.agent_id)).length;
  const briefs = pickupsBy(id, trail);
  return {
    label: 'pickups',
    text: `${adapter} read ${plural(briefs, 'brief')} · ${plural(others, 'handoff')} from other agents (last ${plural(trail.length, 'entry', 'entries')})`,
  };
}

function checkFor(id: string): { lead: string; commands: SlipCommand[] } {
  const name = agentMeta(id).name;
  return {
    lead: `on the machine where you run ${name}:`,
    commands: [
      { prompt: '$', text: doctorCommand(id), label: `Doctor for ${name}` },
      { prompt: '>', text: askAgentDoctor(id), label: `Ask your agent to run remembra_doctor for ${name}`, caption: 'ask your agent:' },
      { prompt: '$', text: pipxRunDoctorCommand(id), label: `Doctor for ${name} without upgrading`, caption: 'no upgrade yet?' },
    ],
  };
}

function unverifiedLine(id: string): string | null {
  const meta = agentMeta(id);
  if (meta.verified || !meta.adapter) return null;
  return `${meta.name}'s adapter is built from its hook docs and has never been run against the real tool.`;
}

function endOneSession(name: string): string {
  return `End one ${name} session; its handoff ticks this row.`;
}

/** The waiting row's state before anyone opens the slip. */
export type RowState = 'connected' | 'codex-trust' | 'briefed' | 'waiting' | 'unverified';

/**
 * Codex skips hooks the user has not trusted, without a message. Until a Codex
 * brief (a pickup) or close (an entry) reaches the trail, the Codex row asks
 * for the trust step.
 */
export function rowState(agentId: string, activity: AgentActivity | undefined, trail: TrailItem[] | undefined): RowState {
  const id = canonicalAgentId(agentId);
  if (activity) return 'connected';
  const items = trail ?? [];
  const briefed = pickupsBy(id, items) > 0;
  // A close on the trail that the summary has not counted yet (they refresh together) still ends the trust state.
  const closed = items.some((item) => isAgent(id, item.agent_id));
  if (id === 'codex' && !briefed && !closed) return 'codex-trust';
  if (briefed && !closed) return 'briefed';
  return agentMeta(id).verified ? 'waiting' : 'unverified';
}

/** First match wins: the verdict table in the Marshal spec (section 7, M1 dashboard). */
export function diagnoseAgent(input: DiagnosisInput): Verdict {
  const id = canonicalAgentId(input.agentId);
  const meta = agentMeta(id);
  const name = meta.name;
  const adapter = meta.adapter ?? id;
  const lines = [
    keysLine(input.keys, input.now),
    entriesLine(id, input.agentTrail, input.summaryAgent, input.now),
    pickupsLine(id, input.trail),
  ];
  const active = input.keys.filter((k) => k.active !== false);
  const used = active.some((k) => k.last_used_at);
  const own = newestFirst(
    [...input.agentTrail, ...input.trail].filter(
      (item, index, all) => isAgent(id, item.agent_id) && all.findIndex((x) => x.id === item.id) === index,
    ),
    (item) => item.created_at,
  );
  const summaryCount = input.summaryAgent ? input.summaryAgent.handoffs + input.summaryAgent.checkpoints : 0;
  const entryCount = Math.max(summaryCount, own.length);
  const ownHandoffs = Math.max(input.summaryAgent?.handoffs ?? 0, own.filter((item) => item.memory_type === 'handoff').length);
  const briefs = pickupsBy(id, input.trail);
  const othersHandoffs = input.trail.filter((item) => item.memory_type === 'handoff' && !isAgent(id, item.agent_id)).length;
  const codexWaiting = id === 'codex' && briefs === 0 && entryCount === 0;
  const codexThen = `${CODEX_TRUST_LINE} ${CODEX_TRUST_CLI}`;
  const doc = id === 'codex' || !meta.verified ? `${RELAY_GUIDE}#setup` : RELAY_GUIDE;

  const build = (v: Omit<Verdict, 'lines' | 'commands' | 'doc'>): Verdict => ({
    ...v,
    lines,
    doc,
    commands: [...(v.fix?.commands ?? []), ...(v.check?.commands ?? [])].map((c) => c.text),
  });

  if (active.length === 0) {
    return build({
      code: 'NO_KEY',
      proven: true,
      verdict: "No relay key yet. Create one above; hooks can't reach Remembra without it.",
      detail: null,
      causes: [],
      unverified: null,
      fix: { text: `Create a relay key in step 1. Then run the one-line install where you run ${name}.`, commands: [] },
      then: codexWaiting ? codexThen : endOneSession(name),
      check: checkFor(id),
      caveat: null,
    });
  }

  if (!used) {
    return build({
      code: 'KEY_NEVER_USED',
      proven: true,
      verdict: 'Your keys have never been used: the install never saved one, or no hook ran.',
      detail: null,
      causes: [],
      unverified: null,
      fix: {
        text: `Run the one-line install on the machine where you run ${name}. It asks for the key at a hidden prompt.`,
        commands: [{ prompt: '$', text: oneLineInstall(input.serverUrl ?? ''), label: 'One-line install and connect' }],
      },
      then: codexWaiting ? codexThen : endOneSession(name),
      check: checkFor(id),
      caveat: KEY_CAVEAT,
    });
  }

  if (briefs > 0 && ownHandoffs === 0) {
    return build({
      code: 'PICKS_UP_NEVER_CLOSES',
      proven: true,
      verdict: `${name} read ${plural(briefs, 'brief')} but never handed off: its close hasn't reached Remembra.`,
      detail: meta.detachClose ? `${name} closes in the background and logs to ${DETACHED_CLOSE_LOG} on that machine.` : null,
      causes: [`a ${name} session is still open: the handoff is written when it ends`, 'the close failed on that machine'],
      unverified: null,
      fix: { text: `End one ${name} session. If no handoff arrives, doctor names the failing close:`, commands: [] },
      then: null,
      check: checkFor(id),
      caveat: null,
    });
  }

  if (codexWaiting) {
    return build({
      code: 'CODEX_TRUST',
      proven: false,
      verdict: CODEX_TRUST_LINE,
      detail: 'Codex skips untrusted hooks without a message, so no brief or handoff from Codex has reached Remembra.',
      causes: [
        'Codex hooks not trusted yet',
        'connect ran as a dry run (the old homepage lines did this)',
        'Codex runs on a machine without the install',
      ],
      unverified: null,
      fix: {
        text: `In the Codex app: Settings > Hooks > Trust. ${CODEX_TRUST_CLI}`,
        commands: [{ prompt: '>', text: '/hooks', label: 'The Codex CLI command that lists hooks to trust', caption: 'in the Codex CLI:' }],
      },
      then: endOneSession('Codex'),
      check: checkFor(id),
      caveat: null,
    });
  }

  if (entryCount === 0 && othersHandoffs === 0) {
    const honest = unverifiedLine(id);
    return build({
      code: 'NOTHING_WAITING',
      proven: true,
      verdict: `${name} hasn't ended a session with the hooks yet, and no handoff was waiting for it.`,
      detail: null,
      causes: [],
      unverified: honest,
      fix: honest
        ? {
            text: `connect --apply leaves ${name}'s hooks out unless you add --include-unverified:`,
            commands: [{ prompt: '$', text: agentConnectCommand(id), label: `Connect command for ${name}` }],
          }
        : { text: endOneSession(name), commands: [] },
      then: honest ? endOneSession(name) : null,
      check: checkFor(id),
      caveat: null,
    });
  }

  if (entryCount === 0 && briefs === 0) {
    const honest = unverifiedLine(id);
    const causes = honest
      ? [
          `connect --apply left ${name}'s hooks out (they need --include-unverified)`,
          'connect ran as a dry run (the old homepage lines did this)',
          `${name} runs on a machine without the install`,
        ]
      : [
          'connect ran as a dry run (the old homepage lines did this)',
          `${name} runs on a machine without the install`,
          `no ${name} session has ended since connect`,
        ];
    const check = checkFor(id);
    return build({
      code: 'HOOKS_NOT_FIRING',
      proven: false,
      verdict: `The key works, but nothing from ${name} has reached Remembra.`,
      detail: null,
      causes,
      unverified: honest,
      fix: honest
        ? {
            text: `Write ${name}'s hooks with --include-unverified, on the machine where you run it:`,
            commands: [{ prompt: '$', text: agentConnectCommand(id), label: `Connect command for ${name}` }],
          }
        : { text: `${check.lead[0].toUpperCase()}${check.lead.slice(1, -1)}, doctor names the cause:`, commands: check.commands },
      then: honest ? endOneSession(name) : null,
      check: honest ? check : null,
      caveat: KEY_CAVEAT,
    });
  }

  const newest = own[0];
  const newestMinutes = newest ? minutesSince(newest.created_at, input.now) : null;
  if (newest && newest.memory_type === 'checkpoint' && newestMinutes !== null && newestMinutes > WORKING_WINDOW_MINUTES) {
    return build({
      code: 'STALE_CHECKPOINT',
      proven: true,
      verdict: `${name}'s last session stopped without a handoff.`,
      detail: `Its newest entry is a checkpoint from ${relativeTime(newest.created_at, input.now)}, with no handoff after it.`,
      causes: [],
      unverified: null,
      fix: {
        text: 'In that repository, write the handoff now:',
        commands: [{ prompt: '$', text: `remembra-relay close --agent ${adapter}`, label: `Close command for ${name}` }],
      },
      then: null,
      check: checkFor(id),
      caveat: null,
    });
  }

  const at = newest?.created_at ?? input.summaryAgent?.last_active ?? null;
  return build({
    code: 'HANDED_OFF',
    proven: true,
    verdict: `${name} is connected: its newest entry arrived ${relativeTime(at, input.now)}.`,
    detail: null,
    causes: [],
    unverified: null,
    fix: null,
    then: null,
    check: null,
    caveat: null,
  });
}

// ---------------------------------------------------------------------------
// Reading: three GETs in parallel, one `›` line as each resolves.
// ---------------------------------------------------------------------------

export type ReadName = 'keys' | 'trail' | 'agentTrail';

export type Read<T> =
  | { status: 'pending' }
  | { status: 'ok'; value: T }
  | { status: 'failed'; httpStatus: number | null };

export interface SlipState {
  keys: Read<KeyEvidence[]>;
  trail: Read<TrailItem[]>;
  agentTrail: Read<TrailItem[]>;
  /** The reads in the order they finished (the order the lines appear). */
  order: ReadName[];
}

export interface SlipSources {
  keys: () => Promise<KeyEvidence[]>;
  trail: () => Promise<TrailItem[]>;
  agentTrail: () => Promise<TrailItem[]>;
}

export const READS: ReadName[] = ['keys', 'trail', 'agentTrail'];

/** The three GETs the slip makes: your active keys, the newest trail entries and the agent's own. */
export function dashboardSources(agentId: string): SlipSources {
  const meta = agentMeta(agentId);
  const adapter = meta.adapter ?? canonicalAgentId(agentId);
  return {
    keys: () => api.listKeys(true).then((res) => res.keys),
    trail: () => relay.trail({ limit: SLIP_TRAIL_LIMIT }).then((res) => res.items),
    agentTrail: () => relay.trail({ agentId: adapter, limit: SLIP_AGENT_LIMIT }).then((res) => res.items),
  };
}

export function initialSlipState(): SlipState {
  return { keys: { status: 'pending' }, trail: { status: 'pending' }, agentTrail: { status: 'pending' }, order: [] };
}

function httpStatusOf(err: unknown): number | null {
  if (err && typeof err === 'object' && 'status' in err && typeof (err as { status: unknown }).status === 'number') {
    return (err as { status: number }).status;
  }
  return null;
}

/**
 * Runs the three reads in parallel and reports the state after each one
 * finishes (success or failure). Resolves with the final state. It only
 * reads: every source is a GET.
 */
export async function readSlip(sources: SlipSources, onChange: (state: SlipState) => void): Promise<SlipState> {
  let state = initialSlipState();
  const settle = <K extends ReadName>(name: K, read: SlipState[K]) => {
    state = { ...state, [name]: read, order: [...state.order, name] };
    onChange(state);
  };
  await Promise.all(
    READS.map((name) =>
      sources[name]().then(
        (value) => settle(name, { status: 'ok', value } as SlipState[typeof name]),
        (err: unknown) => settle(name, { status: 'failed', httpStatus: httpStatusOf(err) }),
      ),
    ),
  );
  return state;
}

function failedText(httpStatus: number | null): string {
  if (httpStatus === 0) return "couldn't reach the Remembra server · check your connection";
  const code = httpStatus ? ` (HTTP ${httpStatus})` : '';
  if (httpStatus === 429) return `couldn't read${code} · try again in a minute`;
  if (httpStatus === 401) return `couldn't read${code} · your session has expired: sign in again`;
  return `couldn't read${code} · try again`;
}

const FAILED_LABEL: Record<ReadName, ReadLine['label']> = { keys: 'keys', trail: 'trail', agentTrail: 'entries' };

export interface SlipOutcome {
  /** One line per finished read, in the order they finished. */
  lines: ReadLine[];
  pending: boolean;
  failed: boolean;
  /** Only when every read succeeded. */
  verdict: Verdict | null;
}

export function slipOutcome(
  state: SlipState,
  ctx: { agentId: string; summaryAgent?: AgentActivity | null; now: Date; serverUrl?: string },
): SlipOutcome {
  const failedLine = (name: ReadName, httpStatus: number | null): ReadLine => ({
    label: FAILED_LABEL[name],
    text: failedText(httpStatus),
    failed: true,
  });
  const lineFor = (name: ReadName): ReadLine | null => {
    if (name === 'keys') {
      const read = state.keys;
      if (read.status === 'ok') return keysLine(read.value, ctx.now);
      return read.status === 'failed' ? failedLine(name, read.httpStatus) : null;
    }
    const read = name === 'trail' ? state.trail : state.agentTrail;
    if (read.status === 'failed') return failedLine(name, read.httpStatus);
    if (read.status === 'pending') return null;
    return name === 'trail'
      ? pickupsLine(ctx.agentId, read.value)
      : entriesLine(ctx.agentId, read.value, ctx.summaryAgent, ctx.now);
  };
  const lines = state.order.map(lineFor).filter((line): line is ReadLine => line !== null);
  const pending = READS.some((name) => state[name].status === 'pending');
  const failed = READS.some((name) => state[name].status === 'failed');
  let verdict: Verdict | null = null;
  if (state.keys.status === 'ok' && state.trail.status === 'ok' && state.agentTrail.status === 'ok') {
    verdict = diagnoseAgent({
      agentId: ctx.agentId,
      summaryAgent: ctx.summaryAgent,
      keys: state.keys.value,
      trail: state.trail.value,
      agentTrail: state.agentTrail.value,
      now: ctx.now,
      serverUrl: ctx.serverUrl,
    });
  }
  return { lines, pending, failed, verdict };
}
