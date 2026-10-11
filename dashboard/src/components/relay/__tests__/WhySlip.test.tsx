// Server-render the real slip and checklist (no DOM needed): every slip state
// (reading, the call, a failed read), the copy lines, the Codex trust row and
// the why? button wiring.

import { readFileSync } from 'node:fs';
import { isValidElement, type ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { MARSHAL_DESK_OFF, MarshalDeskContext, type DeskOpen, type MarshalDeskApi } from '../../../hooks/marshalDesk';
import { SlipAskButton } from '../../marshal/SlipAskButton';
import { TrailNode } from '../Handoff';
import { AgentRow, ConnectChecklist } from '../HomeCards';
import { SlipView, WhySlip } from '../WhySlip';
import { KEY_CAVEAT, SLIP_FOOTER, diagnoseAgent, initialSlipState, type KeyEvidence, type SlipState } from '../../../lib/marshal';
import type { AgentActivity, TrailItem } from '../../../lib/relay';

const NOW = new Date('2026-09-26T12:00:00Z');

const item = (over: Partial<TrailItem> = {}): TrailItem => ({
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

const KEY: KeyEvidence = { name: 'relay (mac, 2026-09-20)', created_at: '2026-09-20T10:00:00Z', last_used_at: '2026-09-26T09:00:00Z', active: true };

function done(keys: KeyEvidence[], trail: TrailItem[], agentTrail: TrailItem[] = []): SlipState {
  return {
    keys: { status: 'ok', value: keys },
    trail: { status: 'ok', value: trail },
    agentTrail: { status: 'ok', value: agentTrail },
    order: ['keys', 'agentTrail', 'trail'],
  };
}

function slip(agentId: string, state: SlipState): string {
  return renderToStaticMarkup(<SlipView id="slip-x" agentId={agentId} state={state} now={NOW} onRetry={() => {}} />);
}

/** The text of each copy line, in order (CopyCommand's one-line span). */
function copied(html: string): string[] {
  return [...html.matchAll(/<span class="whitespace-pre">([^<]*)<\/span>/g)].map((m) =>
    m[1].replace(/&#x27;/g, "'").replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&amp;/g, '&'),
  );
}

/** Visible text (tags stripped, entities decoded, whitespace collapsed). */
function text(html: string): string {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}

describe('SlipView states', () => {
  it('reading: each finished read is a › line, with reading… until the rest are in, and no call yet', () => {
    const state: SlipState = { ...initialSlipState(), trail: { status: 'ok', value: [item()] }, order: ['trail'] };
    const html = slip('codex', state);
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain('aria-busy="true"');
    expect(text(html)).toContain('› pickups codex read 0 briefs · 1 handoff from other agents (last 1 entry)');
    expect(text(html)).toContain('› reading…');
    expect(html).not.toContain('fix →');
    expect(html).not.toContain(SLIP_FOOTER);
    expect(html).toContain('rr-win-line');
    expect(text(html)).toContain('exchange check · codex');
  });

  it('the Codex call: the trust step, marked inferred, its causes, the fix and the doctor lines', () => {
    const state = done([KEY], [item()]);
    const html = slip('codex', state);
    const t = text(html);
    const reads = ['› keys', '› entries', '› pickups'].map((label) => t.indexOf(label));
    expect(reads).toEqual([...reads].sort((a, b) => a - b)); // in the order the reads finished
    expect(t).toContain('= Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust.');
    expect(t).toContain('[??] (inferred, not proven)');
    expect(t).toContain('Likely one of: 1 Codex hooks not trusted yet 2 connect ran as a dry run (the old homepage lines did this)');
    expect(t).toContain(
      'fix → Open Codex Settings > Hooks, or run /hooks in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd.',
    );
    expect(t).toContain('then End one Codex session in a repository. Its handoff ticks this row.');
    expect(t).toContain('check on the machine where you run Codex:');
    expect(t).toContain('ask your agent:');
    expect(t).toContain('no upgrade yet?');
    const verdict = diagnoseAgent({ agentId: 'codex', keys: [KEY], trail: [item()], agentTrail: [], now: NOW });
    expect(copied(html)).toEqual(verdict.commands);
    expect(copied(html)).toEqual([
      '/hooks',
      'remembra-relay doctor --agent codex',
      'run remembra_doctor for codex',
      "pipx run --spec 'remembra>=0.16.1' remembra-relay doctor --agent codex",
    ]);
    // `>` for what is typed into an agent, `$` for the terminal
    const prompts = [...html.matchAll(/<span aria-hidden="true" class="select-none text-signal">([^<]*)<\/span>/g)].map((m) => m[1]);
    expect(prompts).toEqual(['&gt;', '$', '&gt;', '$']);
    expect(html).toContain('href="https://docs.remembra.dev/guides/relay/#codex-trust"'); // the doctor's page for it
    expect(t).toContain(SLIP_FOOTER);
    expect(html).not.toContain(KEY_CAVEAT);
  });

  it('an inferred call that rests on key use carries the one-key caveat', () => {
    const html = slip('claude-code', done([KEY], [item({ agent_id: 'codex' })]));
    const t = text(html);
    expect(t).toContain('= The key works, but nothing from Claude Code has reached Remembra.');
    expect(t).toContain('[??]');
    expect(t).toContain(KEY_CAVEAT);
    expect(t).toContain('fix → On the machine where you run Claude Code, doctor names the cause:');
    expect(copied(html)).toEqual([
      'remembra-relay doctor --agent claude-code',
      'run remembra_doctor for claude-code',
      "pipx run --spec 'remembra>=0.16.1' remembra-relay doctor --agent claude-code",
    ]);
  });

  it('a proven call is marked [!!]', () => {
    const html = slip('claude-code', done([], []));
    expect(text(html)).toContain(
      "= No API key active on your account, so the hooks can't load or save handoffs. [!!] (shown by your data)",
    );
  });

  it('a failed read: the line says why, there is no call, and it can read again', () => {
    const state: SlipState = {
      keys: { status: 'ok', value: [KEY] },
      trail: { status: 'failed', httpStatus: 429 },
      agentTrail: { status: 'ok', value: [] },
      order: ['keys', 'trail', 'agentTrail'],
    };
    const html = slip('codex', state);
    const t = text(html);
    expect(t).toContain("› trail couldn't read (HTTP 429) · try again in a minute");
    expect(t).not.toContain('= ');
    expect(html).not.toContain('fix →');
    expect(t).toContain('No verdict without every read. read again');
    expect(t).toContain(SLIP_FOOTER);
  });

  it('prints a key name as text, never as markup', () => {
    const hostile: KeyEvidence = { ...KEY, name: '<img src=x onerror=alert(1)>' };
    const html = slip('claude-code', done([hostile], []));
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
  });

  it('WhySlip opens in the reading state before any read has finished', () => {
    const html = renderToStaticMarkup(
      <WhySlip
        id="s"
        agentId="codex"
        now={NOW}
        sources={{ keys: async () => [], trail: async () => [], agentTrail: async () => [] }}
      />,
    );
    expect(text(html)).toContain('› reading…');
    expect(html).toContain('aria-busy="true"');
  });

  it('never injects HTML', () => {
    for (const file of ['../WhySlip.tsx', '../HomeCards.tsx', '../../../lib/marshal.ts']) {
      expect(readFileSync(new URL(file, import.meta.url), 'utf8')).not.toContain('dangerouslySetInnerHTML');
    }
  });
});

const ACTIVE: AgentActivity = {
  agent_id: 'claude-code',
  handoffs: 2,
  checkpoints: 0,
  last_active: '2026-09-26T11:00:00Z',
  sessions_7d: 2,
  daily: [],
  projects: ['widget'],
};

function rows(html: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of html.matchAll(/<li data-row-state="([^"]+)"[^>]*>(.*?)<\/li>/gs)) {
    const name = /<span class="block truncate text-sm font-semibold text-ink">([^<]+)<\/span>/.exec(m[2])?.[1] ?? '';
    out[name.split(' ')[0]] = m[1];
  }
  return out;
}

describe('ConnectChecklist rows', () => {
  it('gives every waiting row a why? button wired to its slip, and Codex a dim trust reminder', () => {
    const html = renderToStaticMarkup(<ConnectChecklist agents={[ACTIVE]} now={NOW} trail={[item()]} />);
    expect(rows(html)).toEqual({
      Claude: 'connected',
      Codex: 'codex-waiting',
      Cursor: 'unverified',
      Gemini: 'waiting',
      Qwen: 'waiting',
      Kimi: 'waiting',
    });
    const whys = [...html.matchAll(/<button type="button" id="([^"]+)" aria-expanded="false" aria-controls="([^"]+)"/g)];
    expect(whys).toHaveLength(5); // not on the connected Claude Code row
    for (const [, buttonId, controls] of whys) expect(buttonId).toBe(`${controls}-button`);
    expect(html).toContain('aria-label="Why is Codex waiting?"');
    expect(html).not.toContain('aria-label="Why is Claude Code waiting?"');
    // A Claude-Code-only account has no Codex entry either: the dashboard can't tell whether Codex is
    // installed or its hooks trusted, so the row waits like any other and only reminds, dimly.
    const row = /<li data-row-state="codex-waiting"[^>]*>(.*?)<\/li>/s.exec(html)?.[1] ?? '';
    const codex = text(row);
    expect(codex).toMatch(/^Codex waiting for its first handoff Not connected yet why\? command /);
    expect(codex).toMatch(/using Codex\? trust its 3 hooks, and again when one changes: Codex Settings > Hooks > Trust · \/hooks in the CLI$/);
    expect(text(html)).not.toContain('needs you');
    expect(row).not.toMatch(/text-fail|border-fail|border-dashed/); // no fail colour on an unproven claim
    expect(row).toContain('border-rule');
  });

  it('drops the trust state once a Codex brief or close is on the trail', () => {
    const briefed = renderToStaticMarkup(
      <ConnectChecklist
        agents={[]}
        now={NOW}
        trail={[item({ picked_up_by: [{ agent_id: 'codex', agent_verified: true, picked_up_at: '2026-09-26T10:01:00Z', gap_seconds: 60 }] })]}
      />,
    );
    expect(rows(briefed).Codex).toBe('briefed');
    expect(text(briefed)).toContain('Codex read a brief · waiting for its first handoff');
    expect(text(briefed)).not.toContain('needs you');
    const closed = renderToStaticMarkup(
      <ConnectChecklist agents={[{ ...ACTIVE, agent_id: 'codex' }]} now={NOW} trail={[item({ agent_id: 'codex' })]} />,
    );
    expect(rows(closed).Codex).toBe('connected');
    expect(text(closed)).not.toContain('needs you');
  });

  it('an open row renders its slip under the row, where aria-controls points', () => {
    const html = renderToStaticMarkup(
      <ul>
        <AgentRow
          agentId="codex"
          activity={undefined}
          state="codex-waiting"
          now={NOW}
          open
          onToggle={() => {}}
          onCopy={() => {}}
          idBase="t"
        />
      </ul>,
    );
    expect(html).toContain('aria-expanded="true" aria-controls="t-why-codex"');
    expect(html).toContain('<section id="t-why-codex" aria-label="Exchange check for Codex"');
    expect(html.indexOf('why?')).toBeLessThan(html.indexOf('<section'));
    expect(text(html)).toContain('exchange check · codex');
  });

  it('a connected row has no slip even when asked to open', () => {
    const html = renderToStaticMarkup(
      <ul>
        <AgentRow agentId="claude-code" activity={ACTIVE} state="connected" now={NOW} open onToggle={() => {}} onCopy={() => {}} idBase="t" />
      </ul>,
    );
    expect(html).not.toContain('why?');
    expect(html).not.toContain('<section');
  });
});

describe('ask Marshal about this', () => {
  const inDesk = (desk: Partial<MarshalDeskApi>, state: SlipState) =>
    renderToStaticMarkup(
      <MarshalDeskContext.Provider value={{ ...MARSHAL_DESK_OFF, ...desk }}>
        <SlipView id="slip-x" agentId="codex" state={state} now={NOW} onRetry={() => {}} />
      </MarshalDeskContext.Provider>,
    );

  it('shows after the footer only when the account has the desk and has not turned it off', () => {
    const state = done([KEY], [item()]);
    const on = inDesk({ available: true }, state);
    expect(text(on)).toContain(`${SLIP_FOOTER} ask Marshal about this`);
    expect(on.indexOf('ask Marshal about this')).toBeGreaterThan(on.indexOf(SLIP_FOOTER));
    expect(text(inDesk({}, state))).not.toContain('ask Marshal about this');
    expect(text(inDesk({ available: true, optedOut: true }, state))).not.toContain('ask Marshal about this');
    expect(text(slip('codex', state))).not.toContain('ask Marshal about this'); // no provider: no desk
    // Not while the slip is still reading.
    expect(text(inDesk({ available: true }, initialSlipState()))).not.toContain('ask Marshal about this');
  });

  it('opens the desk prefilled for that agent, without asking', () => {
    const opened: DeskOpen[] = [];
    const button = SlipAskButton({ agentId: 'codex', open: (request) => opened.push(request) });
    expect(isValidElement(button)).toBe(true);
    const invoker = { tagName: 'BUTTON' } as unknown as HTMLElement;
    (button as ReactElement<{ onClick: (e: unknown) => void }>).props.onClick({ currentTarget: invoker });
    expect(opened).toEqual([{ question: 'why is codex waiting', agentId: 'codex', ask: false, source: 'why_slip', invoker }]);
  });
});

describe('rows an evidence chip can point at', () => {
  it('an agent row is agent:<canonical id>, a trail node entry:<id>', () => {
    const row = renderToStaticMarkup(
      <ul>
        <AgentRow agentId="claude" activity={undefined} state="waiting" now={NOW} open={false} onToggle={() => {}} onCopy={() => {}} idBase="t" />
      </ul>,
    );
    expect(row).toContain('data-marshal-ref="agent:claude-code"');
    const node = renderToStaticMarkup(<TrailNode item={item({ id: 'h-42' })} expanded={false} onToggle={() => {}} now={NOW} />);
    expect(node).toContain('data-marshal-ref="entry:h-42"');
  });
});
