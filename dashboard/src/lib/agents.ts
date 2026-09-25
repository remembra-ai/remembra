// Agent identities: display names, monograms and a lane color per agent id.
// Signal orange is reserved for the baton, so lanes use muted hues that hold
// contrast on both the stone paper and the dark panel.

export interface AgentMeta {
  id: string;
  name: string;
  monogram: string;
  lane: string;
  /** The `remembra-relay connect --agent` name, when the relay has an adapter. */
  adapter?: string;
  verified?: boolean;
}

const KNOWN: Record<string, Omit<AgentMeta, 'id'>> = {
  'claude-code': { name: 'Claude Code', monogram: 'CC', lane: '#9a5530', adapter: 'claude-code', verified: true },
  codex: { name: 'Codex', monogram: 'CX', lane: '#356b5d', adapter: 'codex' },
  cursor: { name: 'Cursor', monogram: 'CU', lane: '#4a6096', adapter: 'cursor' },
  gemini: { name: 'Gemini CLI', monogram: 'GE', lane: '#3d74a6', adapter: 'gemini' },
  qwen: { name: 'Qwen Code', monogram: 'QW', lane: '#7a55a0', adapter: 'qwen' },
  kimi: { name: 'Kimi CLI', monogram: 'KI', lane: '#96465e', adapter: 'kimi' },
  dashboard: { name: 'You (dashboard)', monogram: 'YOU', lane: '#5f656b' },
};

const EXTRA_LANES = ['#5f6c33', '#855b19', '#3f6f8f', '#8b5a4a', '#5f6b7a', '#7a4f86'];

/** The one-line install from the landing page. */
export const INSTALL_COMMAND = 'pipx install remembra && remembra-relay connect';

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
