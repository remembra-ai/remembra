// Agent identities: display names, monograms and a lane color per agent id.
// Signal orange is reserved for the baton, so lanes use muted hues that hold
// contrast on both the stone paper and the dark panel.

import type { TrailItem } from './relay';
import { minutesSince } from './time';

export interface AgentMeta {
  id: string;
  name: string;
  monogram: string;
  lane: string;
  /** The `remembra-relay connect --agent` name, when the relay has an adapter. */
  adapter?: string;
  verified?: boolean;
  /** The agent stops its end hook within seconds, so `close` runs detached and logs to last-detached-close.log. */
  detachClose?: boolean;
}

const KNOWN: Record<string, Omit<AgentMeta, 'id'>> = {
  'claude-code': { name: 'Claude Code', monogram: 'CC', lane: '#9a5530', adapter: 'claude-code', verified: true },
  codex: { name: 'Codex', monogram: 'CX', lane: '#356b5d', adapter: 'codex', verified: true, detachClose: true },
  cursor: { name: 'Cursor', monogram: 'CU', lane: '#4a6096', adapter: 'cursor', detachClose: true },
  gemini: { name: 'Gemini CLI', monogram: 'GE', lane: '#3d74a6', adapter: 'gemini', verified: true, detachClose: true },
  qwen: { name: 'Qwen Code', monogram: 'QW', lane: '#7a55a0', adapter: 'qwen', verified: true, detachClose: true },
  kimi: { name: 'Kimi Code', monogram: 'KI', lane: '#96465e', adapter: 'kimi', verified: true },
  dashboard: { name: 'You (dashboard)', monogram: 'YOU', lane: '#5f656b' },
};

const EXTRA_LANES = ['#5f6c33', '#855b19', '#3f6f8f', '#8b5a4a', '#5f6b7a', '#7a4f86'];

/**
 * The first release with `remembra-relay`. Pinning it makes an older PyPI
 * release fail loudly at install time instead of "command not found" later;
 * --force also upgrades an existing older pipx install. The [mcp] extra brings
 * remembra-mcp's dependencies: without it every agent that remembra-install
 * wires up would start a server that cannot import `mcp`. The landing page and
 * docs/guides/relay.md use the same package spec (a Python test checks).
 */
export const PIPX_INSTALL = "pipx install --force 'remembra[mcp]>=0.16'";

/**
 * Saves the key where the relay hooks read it (~/.remembra/credentials) and
 * adds the Remembra MCP server to the agents it finds. The key is never part
 * of the command (shell history keeps commands): remembra-install asks for it
 * at a hidden prompt, shows each change and writes after a "y". `serverUrl`
 * is the server the key belongs to; the dashboard always passes its own.
 * Without one, remembra-install keeps the server the machine already uses, or
 * Remembra Cloud on a first install: the line remembra.dev and setup.md show.
 */
export function saveKeyCommand(serverUrl: string): string {
  return serverUrl ? `remembra-install --all --url ${serverUrl}` : 'remembra-install --all';
}

/**
 * The whole first run on one line: install, save the key (asked for at a
 * hidden prompt, never on the command line) and add the MCP server to the
 * agents it finds, then write the hooks. remembra-install exits 3 when the
 * user answers no (or nothing was written), so `&&` stops before
 * `connect --apply` writes any hook.
 */
export function oneLineInstall(serverUrl: string): string {
  return `${PIPX_INSTALL} && ${saveKeyCommand(serverUrl)} && remembra-relay connect --apply`;
}

/** Taking Remembra off a machine, in order (docs/guides/relay.md#uninstall says the same). */
export const UNINSTALL_STEPS: { command: string; what: string }[] = [
  { command: 'remembra-relay disconnect --apply', what: 'removes the session hooks from every agent (a backup of each file is kept)' },
  { command: 'remembra-install --remove --all --apply', what: 'removes the Remembra MCP server from every agent' },
  { command: 'pipx uninstall remembra', what: 'removes the commands' },
  { command: 'rm -r ~/.remembra', what: 'deletes the saved key, the unsent-handoff queue and the log' },
];

/**
 * Writes one agent's session hooks. An unverified adapter (built from the
 * tool's docs, never run against it) is written only with --include-unverified.
 */
export function agentConnectCommand(agentId: string): string {
  const meta = agentMeta(agentId);
  const adapter = meta.adapter ?? canonicalAgentId(agentId);
  if (meta.verified) return `remembra-relay connect --apply --agent ${adapter}`;
  return `remembra-relay connect --apply --agent ${adapter} --include-unverified`;
}

/** The first release with `remembra-relay doctor`: the pipx-run form below works on an older install. */
export const DOCTOR_RELEASE = '0.16.1';

/**
 * Marshal's read-only check of this machine: the key, the unsent-handoff
 * queue, each agent's hooks (and Codex's trust records) and the trail. It
 * prints the one fix and changes nothing.
 */
export function doctorCommand(agentId?: string | null): string {
  const adapter = agentId ? agentMeta(agentId).adapter ?? canonicalAgentId(agentId) : null;
  return adapter ? `remembra-relay doctor --agent ${adapter}` : 'remembra-relay doctor';
}

/** The same check without upgrading first: pipx runs the newest release once, in a throwaway environment. */
export function pipxRunDoctorCommand(agentId?: string | null): string {
  return `pipx run --spec 'remembra>=${DOCTOR_RELEASE}' ${doctorCommand(agentId)}`;
}

/** What to ask an agent that has the Remembra MCP server: its remembra_doctor tool runs the same check. */
export function askAgentDoctor(agentId?: string | null): string {
  const adapter = agentId ? agentMeta(agentId).adapter ?? canonicalAgentId(agentId) : null;
  return adapter ? `run remembra_doctor for ${adapter}` : 'run remembra_doctor';
}

/** The agents `remembra-relay connect` can wire up, in checklist order. */
export const CONNECTABLE_AGENTS = ['claude-code', 'codex', 'cursor', 'gemini', 'qwen', 'kimi'];

const ALIASES: Record<string, string> = {
  claude: 'claude-code',
  'claude code': 'claude-code',
  claude_code: 'claude-code',
  'codex-cli': 'codex',
  'openai-codex': 'codex',
  'gemini-cli': 'gemini',
  'qwen-code': 'qwen',
  'kimi-cli': 'kimi',
  'kimi-code': 'kimi',
};

function hash(text: string): number {
  let h = 0;
  for (let i = 0; i < text.length; i += 1) h = (h * 31 + text.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function titleCase(id: string): string {
  return id
    .split(/[-_./:@+\s]+/)
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

/** Names as a sentence lists them: "A", "A and B", "A, B and C". */
export function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** Canonical id for known agents ("Claude" -> "claude-code"); others unchanged. */
export function canonicalAgentId(id: string | null | undefined): string {
  const raw = (id || '').trim();
  const key = raw.toLowerCase();
  if (KNOWN[key]) return key;
  return ALIASES[key] ?? raw;
}

export function agentMeta(id: string | null | undefined): AgentMeta {
  const raw = (id || '').trim();
  if (!raw) return { id: '', name: 'Unattributed', monogram: '?', lane: '#5f656b' };
  const canonical = canonicalAgentId(raw);
  const known = KNOWN[canonical];
  if (known) return { id: raw, ...known };
  const name = titleCase(raw) || raw;
  const letters = name.replace(/[^A-Za-z0-9 ]/g, '').split(' ').filter(Boolean);
  const monogram = (letters.length > 1 ? letters[0][0] + letters[1][0] : name.slice(0, 2)).toUpperCase();
  return { id: raw, name, monogram, lane: EXTRA_LANES[hash(raw) % EXTRA_LANES.length] };
}

/** A checkpoint newer than this (with no later handoff) counts as "working now". */
export const WORKING_WINDOW_MINUTES = 60;

export type AgentState =
  | { kind: 'working'; at: string }
  | { kind: 'handed-off'; at: string }
  | { kind: 'recent' }
  | { kind: 'idle' };

/**
 * What an agent card may claim. A handoff is written when a session closes,
 * so only a recent checkpoint that is the agent's newest entry (no handoff
 * after it) means the agent is still working. ``newest`` is the agent's
 * newest entry on the loaded trail page, if any; ``lastActive`` is the
 * summary's newest entry time for the agent.
 */
export function agentState(lastActive: string | null, newest: TrailItem | undefined, now: Date): AgentState {
  const newestMinutes = newest ? minutesSince(newest.created_at, now) : null;
  if (newest && newestMinutes !== null && newestMinutes <= WORKING_WINDOW_MINUTES) {
    return newest.memory_type === 'checkpoint'
      ? { kind: 'working', at: newest.created_at }
      : { kind: 'handed-off', at: newest.created_at };
  }
  const minutes = minutesSince(lastActive, now);
  if (minutes !== null && minutes <= WORKING_WINDOW_MINUTES) return { kind: 'recent' };
  return { kind: 'idle' };
}
