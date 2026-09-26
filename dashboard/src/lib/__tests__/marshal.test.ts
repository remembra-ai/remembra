// Marshal's "why?" rules: every case in the shared fixture, the row states
// (Codex's trust step until a brief or close arrives), the three reads as
// they finish, and the real API client making only the three GETs.

import { readFileSync } from 'node:fs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from '../api';
import {
  DOCTOR_RELEASE,
  agentConnectCommand,
  askAgentDoctor,
  doctorCommand,
  oneLineInstall,
  pipxRunDoctorCommand,
} from '../agents';
import {
  CODEX_TRUST_FIX,
  CODEX_TRUST_LINE,
  RELAY_GUIDE,
  dashboardSources,
  diagnoseAgent,
  initialSlipState,
  readSlip,
  rowState,
  say,
  slipOutcome,
  type KeyEvidence,
  type SlipSources,
  type SlipState,
  type Verdict,
  type VerdictCode,
} from '../marshal';
import { CODEX_TRUST_EVENTS, DASHBOARD_ONLY, SHARED_RULES, UNVERIFIED } from '../marshalWords';
import type { AgentActivity, TrailItem } from '../relay';

interface FixtureCase {
  name: string;
  input: {
    agent_id: string;
    keys: KeyEvidence[];
    trail: TrailItem[];
    agent_trail: TrailItem[];
    summary_agent?: AgentActivity | null;
  };
  expect: Partial<{
    code: VerdictCode;
    proven: boolean;
    verdict: string;
    detail: string | null;
    lines: string[];
    causes: string[];
    unverified: string | null;
    fix: string | null;
    then: string | null;
    commands: string[];
    caveat: string | null;
    doc: string;
  }>;
}

const FIXTURE: { now: string; server_url: string; cases: FixtureCase[] } = JSON.parse(
  readFileSync(new URL('../../../../tests/fixtures/marshal/diagnosis_cases.json', import.meta.url), 'utf8'),
);
const NOW = new Date(FIXTURE.now);

function run(c: FixtureCase): Verdict {
  return diagnoseAgent({
    agentId: c.input.agent_id,
    summaryAgent: c.input.summary_agent ?? null,
    keys: c.input.keys,
    trail: c.input.trail,
    agentTrail: c.input.agent_trail,
    now: NOW,
    serverUrl: FIXTURE.server_url,
  });
}

describe('diagnoseAgent: the shared fixture', () => {
  it.each(FIXTURE.cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    const v = run(c);
    const want = c.expect;
    if (want.code !== undefined) expect(v.code).toBe(want.code);
    if (want.proven !== undefined) expect(v.proven).toBe(want.proven);
    if (want.verdict !== undefined) expect(v.verdict).toBe(want.verdict);
    if (want.detail !== undefined) expect(v.detail).toBe(want.detail);
    if (want.lines !== undefined) expect(v.lines.map((l) => `${l.label}: ${l.text}`)).toEqual(want.lines);
    if (want.causes !== undefined) expect(v.causes).toEqual(want.causes);
    if (want.unverified !== undefined) expect(v.unverified).toBe(want.unverified);
    if (want.fix !== undefined) expect(v.fix?.text ?? null).toBe(want.fix);
    if (want.then !== undefined) expect(v.then).toBe(want.then);
    if (want.commands !== undefined) expect(v.commands).toEqual(want.commands);
    if (want.caveat !== undefined) expect(v.caveat).toBe(want.caveat);
    if (want.doc !== undefined) expect(v.doc).toBe(want.doc);
  });

  it('covers every verdict: each rule it shares with the doctor, and the two only a slip reaches', () => {
    const codes = new Set(FIXTURE.cases.map((c) => run(c).code));
    const table: VerdictCode[] = [...(Object.keys(SHARED_RULES) as VerdictCode[]), ...DASHBOARD_ONLY];
    expect([...codes].sort()).toEqual([...table].sort());
    expect(table).toEqual(
      expect.arrayContaining(['KEY_MISSING', 'PICKS_UP_NEVER_CLOSES', 'CODEX_TRUST_MISSING', 'STALE_CHECKPOINT']),
    );
  });

  it("says a shared rule in the doctor's words, and links the doctor's page for it", () => {
    for (const c of FIXTURE.cases) {
      const v = run(c);
      if (!(v.code in SHARED_RULES)) continue;
      const rule = SHARED_RULES[v.code as keyof typeof SHARED_RULES];
      expect(v.doc, c.name).toBe(`${RELAY_GUIDE}${rule.doc}`);
      // The call is the shared template with only its {values} filled in.
      const literal = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const shape = new RegExp(`^${rule.what.split(/\{\w+\}/).map(literal).join('.*?')}$`);
      expect(v.verdict, c.name).toMatch(shape);
    }
    expect(CODEX_TRUST_LINE).toBe('Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust.');
    expect(CODEX_TRUST_FIX).toBe(
      'Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd.',
    );
    expect(CODEX_TRUST_EVENTS).toEqual(['SessionStart', 'UserPromptSubmit', 'SessionEnd']);
    expect(say(UNVERIFIED, { name: 'Cursor' })).toBe(
      "Cursor's adapter is built from its hook docs and has never been run against the real tool.",
    );
  });

  it('fills a template only with the values it names, and refuses a missing one', () => {
    expect(say('{name} read {briefs}.', { name: 'Codex', briefs: '2 briefs' })).toBe('Codex read 2 briefs.');
    expect(() => say(SHARED_RULES.HOOKS_NOT_FIRING.what, { name: 'Codex' })).toThrow('no value for {since}');
  });

  it('marks every inference [??] and gives the one-key caveat wherever the call rests on key use', () => {
    for (const c of FIXTURE.cases) {
      const v = run(c);
      if (v.code === 'HOOKS_NOT_FIRING' || v.code === 'KEY_NEVER_USED') expect(v.caveat, c.name).not.toBeNull();
      if (v.code === 'HOOKS_NOT_FIRING' || v.code === 'CODEX_TRUST_MISSING') {
        expect(v.proven, c.name).toBe(false);
        expect(v.causes.length, c.name).toBeGreaterThan(0);
      }
    }
  });

  it('writes in the Marshal voice: short calls, no first person, no exclamation, no chat filler', () => {
    const banned = ['How can I help', "I'm here to help", 'Great question', 'Sure!', 'AI Assistant', '✨'];
    for (const c of FIXTURE.cases) {
      const v = run(c);
      const prose = [v.verdict, v.detail, v.unverified, v.fix?.text, v.then, v.check?.lead, v.caveat, ...v.causes].filter(
        (t): t is string => !!t,
      );
      for (const text of prose) {
        for (const phrase of banned) expect(text, c.name).not.toContain(phrase);
        expect(text, c.name).not.toMatch(/!(?!!)|\bI\b|\bI'm\b/);
        for (const sentence of text.split(/(?<=\.)\s+/)) expect(sentence.split(/\s+/).length, sentence).toBeLessThan(20);
      }
    }
  });

  it('only ever offers commands the templates build', () => {
    const allowed = [
      /^remembra-relay doctor --agent [a-z-]+$/,
      /^run remembra_doctor for [a-z-]+$/,
      new RegExp(`^pipx run --spec 'remembra>=${DOCTOR_RELEASE.replace(/\./g, '\\.')}' remembra-relay doctor --agent [a-z-]+$`),
      /^remembra-relay connect --apply --agent [a-z-]+( --include-unverified)?$/,
      /^remembra-relay close --agent [a-z-]+$/,
      /^\/hooks$/,
    ];
    for (const c of FIXTURE.cases) {
      for (const cmd of run(c).commands) {
        if (cmd === oneLineInstall(FIXTURE.server_url)) continue;
        expect(
          allowed.some((re) => re.test(cmd)),
          `${c.name}: ${cmd}`,
        ).toBe(true);
        expect(cmd).not.toMatch(/rem_[A-Za-z0-9_-]{8,}/);
      }
    }
  });
});

describe('command templates', () => {
  it('builds doctor, its pipx-run form for old installs and the agent ask', () => {
    expect(doctorCommand()).toBe('remembra-relay doctor');
    expect(doctorCommand('codex')).toBe('remembra-relay doctor --agent codex');
    expect(doctorCommand('Claude Code')).toBe('remembra-relay doctor --agent claude-code');
    expect(pipxRunDoctorCommand('codex')).toBe("pipx run --spec 'remembra>=0.16.1' remembra-relay doctor --agent codex");
    expect(pipxRunDoctorCommand()).toBe("pipx run --spec 'remembra>=0.16.1' remembra-relay doctor");
    expect(askAgentDoctor('codex')).toBe('run remembra_doctor for codex');
    expect(agentConnectCommand('codex')).toBe('remembra-relay connect --apply --agent codex');
    expect(agentConnectCommand('kimi')).toBe('remembra-relay connect --apply --agent kimi');
    expect(agentConnectCommand('cursor')).toBe('remembra-relay connect --apply --agent cursor --include-unverified');
  });
});

const trailItem = (over: Partial<TrailItem>): TrailItem => ({
  id: 'h1',
  project_id: 'widget',
  memory_type: 'handoff',
  agent_id: 'claude-code',
  session_id: 's1',
  created_at: '2026-09-26T10:00:00Z',
  branch: 'main',
  head_commit: null,
  headline: 'entry',
  failing: 0,
  open: 0,
  picked_up_by: [],
  ...over,
});

const pickup = (agent: string) => ({ agent_id: agent, agent_verified: true, picked_up_at: '2026-09-26T10:05:00Z', gap_seconds: 300 });

describe('rowState: the checklist row before the slip opens', () => {
  const activity: AgentActivity = {
    agent_id: 'codex',
    handoffs: 1,
    checkpoints: 0,
    last_active: '2026-09-26T11:00:00Z',
    sessions_7d: 1,
    daily: [],
    projects: ['widget'],
  };

  it('keeps the Codex trust reminder until a Codex brief or close arrives', () => {
    expect(rowState('codex', undefined, undefined)).toBe('codex-waiting');
    expect(rowState('codex', undefined, [trailItem({})])).toBe('codex-waiting');
    // a brief: Codex was served another agent's handoff
    expect(rowState('codex', undefined, [trailItem({ picked_up_by: [pickup('codex')] })])).toBe('briefed');
    // an alias of Codex counts
    expect(rowState('codex', undefined, [trailItem({ picked_up_by: [pickup('openai-codex')] })])).toBe('briefed');
    // a close on the trail the summary has not counted yet
    expect(rowState('codex', undefined, [trailItem({ agent_id: 'codex', memory_type: 'checkpoint' })])).toBe('waiting');
    expect(rowState('codex', activity, [])).toBe('connected');
  });

  it('shows other agents as briefed, waiting or unverified', () => {
    expect(rowState('claude-code', undefined, [trailItem({ agent_id: 'codex', picked_up_by: [pickup('claude')] })])).toBe('briefed');
    expect(rowState('claude-code', undefined, [])).toBe('waiting');
    expect(rowState('cursor', undefined, [])).toBe('unverified');
  });

  it('matches the verdict: the Codex row opens on the trust call, inferred', () => {
    const v = diagnoseAgent({
      agentId: 'codex',
      keys: [{ name: 'k', created_at: '2026-09-20T00:00:00Z', last_used_at: '2026-09-26T09:00:00Z', active: true }],
      trail: [trailItem({})],
      agentTrail: [],
      now: new Date('2026-09-26T12:00:00Z'),
    });
    expect(v.code).toBe('CODEX_TRUST_MISSING');
    expect(v.verdict).toBe(CODEX_TRUST_LINE);
    expect(v.proven).toBe(false); // [??]: the slip can't see Codex; doctor on that machine can
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const USED_KEY: KeyEvidence = { name: 'relay (mac)', created_at: '2026-09-20T00:00:00Z', last_used_at: '2026-09-26T09:00:00Z', active: true };
const CTX = { agentId: 'codex', now: new Date('2026-09-26T12:00:00Z') };

describe('readSlip: three reads, one line as each finishes', () => {
  it('shows the lines in the order the reads finish, and the call only when all three are in', async () => {
    const keys = deferred<KeyEvidence[]>();
    const trail = deferred<TrailItem[]>();
    const agentTrail = deferred<TrailItem[]>();
    const sources: SlipSources = { keys: () => keys.promise, trail: () => trail.promise, agentTrail: () => agentTrail.promise };
    const seen: SlipState[] = [];
    const done = readSlip(sources, (s) => seen.push(s));

    trail.resolve([trailItem({})]);
    await Promise.resolve();
    await Promise.resolve();
    let out = slipOutcome(seen[seen.length - 1], CTX);
    expect(out.lines.map((l) => l.label)).toEqual(['pickups']);
    expect(out.pending).toBe(true);
    expect(out.verdict).toBeNull();

    keys.resolve([USED_KEY]);
    agentTrail.resolve([]);
    const final = await done;
    out = slipOutcome(final, CTX);
    expect(final.order).toEqual(['trail', 'keys', 'agentTrail']);
    expect(out.lines.map((l) => l.label)).toEqual(['pickups', 'keys', 'entries']);
    expect(out.pending).toBe(false);
    expect(out.verdict?.code).toBe('CODEX_TRUST_MISSING');
    expect(seen).toHaveLength(3);
  });

  it('names a failed read and gives no verdict', async () => {
    const sources: SlipSources = {
      keys: async () => [USED_KEY],
      trail: async () => {
        throw new ApiError('Too many requests', 429);
      },
      agentTrail: async () => [],
    };
    const out = slipOutcome(await readSlip(sources, () => {}), CTX);
    expect(out.failed).toBe(true);
    expect(out.verdict).toBeNull();
    expect(out.lines.find((l) => l.failed)).toEqual({
      label: 'trail',
      text: "couldn't read (HTTP 429) · try again in a minute",
      failed: true,
    });
  });

  it('says what to do for an expired session and an unreachable server', async () => {
    const failing = (status: number): SlipSources => ({
      keys: async () => {
        throw new ApiError('nope', status);
      },
      trail: async () => [],
      agentTrail: async () => {
        throw new ApiError('nope', status);
      },
    });
    const expired = slipOutcome(await readSlip(failing(401), () => {}), CTX);
    expect(expired.lines.filter((l) => l.failed).map((l) => `${l.label}: ${l.text}`)).toEqual([
      "keys: couldn't read (HTTP 401) · your session has expired: sign in again",
      "entries: couldn't read (HTTP 401) · your session has expired: sign in again",
    ]);
    const offline = slipOutcome(await readSlip(failing(0), () => {}), CTX);
    expect(offline.lines.find((l) => l.failed)?.text).toBe("couldn't reach the Remembra server · check your connection");
  });

  it('starts empty: nothing read, nothing claimed', () => {
    const out = slipOutcome(initialSlipState(), CTX);
    expect(out).toEqual({ lines: [], pending: true, failed: false, verdict: null });
  });
});

describe('dashboardSources: the real API client makes three GETs and nothing else', () => {
  const store = new Map<string, string>();
  const calls: { url: string; method: string }[] = [];

  beforeEach(() => {
    store.clear();
    calls.length = 0;
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    vi.stubGlobal('fetch', async (url: string, init: RequestInit = {}) => {
      calls.push({ url, method: init.method ?? 'GET' });
      const body = url.startsWith('/api/v1/keys')
        ? { keys: [USED_KEY], count: 1 }
        : url.includes('agent_id=codex')
          ? { items: [], total: 0, project_id: null }
          : { items: [trailItem({ picked_up_by: [] })], total: 1, project_id: null };
      return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    });
    api.setJwtToken('test-session');
  });

  afterEach(() => {
    api.clearAll();
    vi.unstubAllGlobals();
  });

  it('reads keys, the newest 100 entries and the agent’s newest 5, then calls it', async () => {
    const final = await readSlip(dashboardSources('codex'), () => {});
    expect(calls.map((c) => c.method)).toEqual(['GET', 'GET', 'GET']);
    expect(calls.map((c) => c.url).sort()).toEqual([
      '/api/v1/keys?active_only=true',
      '/api/v1/trail?agent_id=codex&limit=5&offset=0',
      '/api/v1/trail?limit=100&offset=0',
    ]);
    for (const { url } of calls) {
      expect(url).not.toMatch(/session\/brief|projects\/resolve|memories\/recall/);
    }
    const out = slipOutcome(final, CTX);
    expect(out.verdict?.code).toBe('CODEX_TRUST_MISSING');
  });
});
