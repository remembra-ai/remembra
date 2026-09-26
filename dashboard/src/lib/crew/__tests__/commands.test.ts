import { describe, expect, it } from 'vitest';
import { CrewApiError } from '../api';
import {
  CREW_COMMANDS,
  REASON_MAX,
  answer,
  back,
  flowErrorMessage,
  nextStep,
  runFlow,
  startFlow,
  validateText,
  type CrewFlow,
  type FlowContext,
} from '../commands';
import { applyEvent, fromSnapshot } from '../reducer';
import type { CrewListItem, CrewSnapshot, CrewState } from '../types';
import { reducerVectors } from './vectors';

const SNAP: CrewSnapshot = reducerVectors().find((v) => v.name === 'snapshot_init')!.snapshot;
const CREW = SNAP.crew.id;

function listItem(): CrewListItem {
  return {
    crew: SNAP.crew,
    role: 'owner',
    live: 2,
    needs_you: 1,
    crew_inbox: 0,
    moments_24h: 0,
    last_event_at: null,
    tasks_by_status: {},
    phases: [],
    live_sessions: [],
    live_sessions_truncated: false,
  };
}

function ctx(state: CrewState | null = fromSnapshot(SNAP)): FlowContext {
  return { crews: [listItem()], stateOf: (id) => (id === CREW ? state : null) };
}

class FakeActions {
  calls: [string, ...unknown[]][] = [];
  fail: CrewApiError | null = null;
  private record(name: string, ...args: unknown[]) {
    this.calls.push([name, ...args]);
    if (this.fail) throw this.fail;
  }
  freezeZone = async (zoneId: string, reason: string) => {
    this.record('freezeZone', zoneId, reason);
    return {};
  };
  pauseSession = async (sessionId: string, reason: string) => {
    this.record('pauseSession', sessionId, reason);
    return {};
  };
  requestCheckpoint = async (sessionId: string, reason: string) => {
    this.record('requestCheckpoint', sessionId, reason);
    return {};
  };
  postMessage = async (crewId: string, body: { kind?: string; body: string }) => {
    this.record('postMessage', crewId, body);
    return {};
  };
  issueBypassCode = async (crewId: string, body: { session_id: string; scope: string; minutes: number }) => {
    this.record('issueBypassCode', crewId, body);
    return { code_id: 'byp_1', code: 'RCB-ABCDE-FGHJK', session_id: body.session_id, scope: body.scope, expires_at: '2026-09-25T20:15:00Z' };
  };
}

function walk(flow: CrewFlow, context: FlowContext, answers: string[]): CrewFlow {
  let f = flow;
  for (const value of answers) {
    const next = answer(f, context, value);
    expect(next, `answer ${value} to ${JSON.stringify(nextStep(f, context))}`).not.toBe(f);
    f = next;
  }
  return f;
}

describe('crew palette commands', () => {
  it('offers the seven §9.1 commands', () => {
    expect(CREW_COMMANDS.map((c) => c.label)).toEqual([
      'Go to crew…',
      'Who holds…',
      'Freeze zone…',
      'Pause agent…',
      'Request checkpoint…',
      'Post to crew…',
      'Issue bypass code…',
    ]);
    expect(CREW_COMMANDS.filter((c) => c.humanOnly).map((c) => c.id)).toEqual(['freeze', 'pause', 'checkpoint', 'bypass']);
  });

  it('asks for the crew first unless the page already is one', () => {
    const c = ctx();
    const step = nextStep(startFlow('go'), c);
    expect(step).toMatchObject({ kind: 'pick', field: 'crew' });
    if (step.kind !== 'pick') throw new Error();
    expect(step.options).toEqual([{ value: CREW, label: 'YaadBooks', description: 'yaadbooks · 2 live · 1 needs you' }]);
    expect(nextStep(startFlow('go', { crewId: CREW, project: 'yaadbooks' }), c)).toEqual({ kind: 'ready' });
  });

  it('go → navigates to Mission Control', async () => {
    const c = ctx();
    const flow = walk(startFlow('go'), c, [CREW]);
    expect(await runFlow(flow, c, new FakeActions())).toEqual({ message: 'Opening yaadbooks', href: '#/crew?project=yaadbooks' });
  });

  it('who holds → lists zones with their holder and opens the zone', async () => {
    const c = ctx();
    const flow = startFlow('who-holds', { crewId: CREW, project: 'yaadbooks' });
    const step = nextStep(flow, c);
    if (step.kind !== 'pick') throw new Error('expected a zone pick');
    const pos = step.options.find((o) => o.label === 'pos')!;
    expect(pos.description).toMatch(/^held EXCLUSIVELY by cc-1 for T-14 · active/);
    const done = walk(flow, c, [pos.value]);
    const result = await runFlow(done, c, new FakeActions());
    expect(result.href).toBe('#/crew?project=yaadbooks&view=zones&zone=pos');
    expect(result.message).toContain('pos: held EXCLUSIVELY by cc-1');
  });

  it('waits for the crew state before zone and session steps', () => {
    const flow = startFlow('freeze', { crewId: CREW, project: 'yaadbooks' });
    expect(nextStep(flow, ctx(null))).toEqual({ kind: 'loading', title: 'Loading yaadbooks…' });
    expect(answer(flow, ctx(null), 'zn_pos')).toBe(flow);
  });

  it('freeze → zone, reason, then POST /zones/{id}/freeze; frozen and built-in zones cannot be picked', async () => {
    let state = fromSnapshot(SNAP);
    const reports = state.zones.zn_reports;
    state = { ...state, zones: { ...state.zones, zn_reports: { ...reports, frozen_by: 'u_mani' } } };
    const c = ctx(state);
    const flow = startFlow('freeze', { crewId: CREW, project: 'yaadbooks' });
    const step = nextStep(flow, c);
    if (step.kind !== 'pick') throw new Error();
    expect(step.options.find((o) => o.value === 'zn_reports')?.disabled).toBe('already frozen');
    expect(answer(flow, c, 'zn_reports')).toBe(flow);
    const withZone = walk(flow, c, ['zn_pos']);
    const reasonStep = nextStep(withZone, c);
    expect(reasonStep).toMatchObject({ kind: 'text', maxLength: REASON_MAX });
    expect(answer(withZone, c, '   ')).toBe(withZone); // a reason is required
    const ready = walk(withZone, c, ['  Mani is editing POS himself  ']);
    const api = new FakeActions();
    const result = await runFlow(ready, c, api);
    expect(api.calls).toEqual([['freezeZone', 'zn_pos', 'Mani is editing POS himself']]);
    expect(result.message).toBe('Froze zone pos. Agents are denied there until you unfreeze it.');
  });

  it('pause and checkpoint → pick a live session and a reason', async () => {
    const c = ctx();
    const api = new FakeActions();
    const pause = walk(startFlow('pause', { crewId: CREW, project: 'yaadbooks' }), c, ['cs_b', 'wrong branch']);
    expect((await runFlow(pause, c, api)).message).toBe('Paused codex-1. Its next write is denied.');
    const ckpt = walk(startFlow('checkpoint', { crewId: CREW, project: 'yaadbooks' }), c, ['cs_a', 'before lunch']);
    expect((await runFlow(ckpt, c, api)).message).toBe('Asked cc-1 for a checkpoint.');
    expect(api.calls).toEqual([
      ['pauseSession', 'cs_b', 'wrong branch'],
      ['requestCheckpoint', 'cs_a', 'before lunch'],
    ]);
  });

  it('a paused session cannot be paused again; ended sessions are not offered', () => {
    let state = fromSnapshot(SNAP);
    state = applyEvent(state, { seq: state.last_seq + 1, crew_id: CREW, type: 'session.paused', refs: { session_id: 'cs_a' }, payload: { reason: 'x' } });
    state = applyEvent(state, { seq: state.last_seq + 1, crew_id: CREW, type: 'session.left', refs: { session_id: 'cs_b' }, payload: { reason: 'done' } });
    const step = nextStep(startFlow('pause', { crewId: CREW, project: 'yaadbooks' }), ctx(state));
    if (step.kind !== 'pick') throw new Error();
    expect(step.options.map((o) => [o.value, o.disabled])).toEqual([['cs_a', 'already paused']]);
  });

  it('post → message text, then POST /crews/{id}/messages as chat', async () => {
    const c = ctx();
    const flow = walk(startFlow('post'), c, [CREW, 'Hold POS until T-14 lands']);
    const api = new FakeActions();
    const result = await runFlow(flow, c, api);
    expect(api.calls).toEqual([['postMessage', CREW, { kind: 'chat', body: 'Hold POS until T-14 lands' }]]);
    expect(result).toEqual({ message: 'Posted to yaadbooks.', href: '#/crew?project=yaadbooks&view=channel' });
  });

  it('bypass → session, scope, minutes; the code is returned once to copy', async () => {
    const c = ctx();
    const flow = startFlow('bypass', { crewId: CREW, project: 'yaadbooks' });
    const withSession = walk(flow, c, ['cs_b']);
    const scopes = nextStep(withSession, c);
    if (scopes.kind !== 'pick') throw new Error();
    expect(scopes.options.map((o) => o.value)).toEqual(['commit', 'push', 'write:pos', 'write:reports']);
    const ready = walk(withSession, c, ['write:pos', '10']);
    const api = new FakeActions();
    const result = await runFlow(ready, c, api);
    expect(api.calls).toEqual([['issueBypassCode', CREW, { session_id: 'cs_b', scope: 'write:pos', minutes: 10 }]]);
    expect(result.copy).toBe('RCB-ABCDE-FGHJK');
    expect(result.message).toContain('codex-1 (write:pos), single use');
  });

  it('refuses to run an unfinished flow', async () => {
    await expect(runFlow(startFlow('freeze', { crewId: CREW, project: 'yaadbooks' }), ctx(), new FakeActions())).rejects.toThrow(
      'flow is not ready',
    );
  });

  it('back() undoes the last answer, and keeps a crew the page chose', () => {
    const c = ctx();
    const full = walk(startFlow('bypass'), c, [CREW, 'cs_a', 'push', '5']);
    const steps: (CrewFlow | null)[] = [];
    let f: CrewFlow | null = full;
    while (f) {
      f = back(f, false);
      steps.push(f);
    }
    expect(steps.map((s) => (s ? nextStep(s, c).kind === 'pick' && (nextStep(s, c) as { field: string }).field : null))).toEqual([
      'minutes',
      'scope',
      'session',
      'crew',
      null,
    ]);
    expect(back(startFlow('go', { crewId: CREW, project: 'yaadbooks' }), true)).toBeNull();
  });

  it('validates text length in bytes', () => {
    const step = { kind: 'text' as const, field: 'text' as const, title: '', placeholder: '', maxLength: 4 };
    expect(validateText(step, 'abcd')).toBeNull();
    expect(validateText(step, 'abcde')).toBe('Too long (max 4 characters).');
    expect(validateText(step, 'ééé')).toBe('Too long (max 4 characters).');
    expect(validateText({ ...step, maxLength: REASON_MAX }, ' ')).toBe('Give a reason.');
    expect(validateText(step, '')).toBe('Write a message.');
  });

  it('explains refusals in plain words', () => {
    expect(flowErrorMessage(new CrewApiError('x', 401, 'step_up_required'))).toMatch(/Sign in again/);
    expect(flowErrorMessage(new CrewApiError('x', 403, 'human_only'))).toMatch(/dashboard login/);
    expect(flowErrorMessage(new CrewApiError('x', 404, 'not_found'))).toMatch(/Not found/);
    expect(flowErrorMessage(new CrewApiError('Zone pos is protected.', 423, 'protected'))).toBe('Zone pos is protected.');
    expect(flowErrorMessage(new Error('boom'))).toBe('boom');
  });

  it('surfaces a server refusal from the action', async () => {
    const c = ctx();
    const api = new FakeActions();
    api.fail = new CrewApiError('Sign in again.', 401, 'step_up_required');
    const flow = walk(startFlow('bypass', { crewId: CREW, project: 'yaadbooks' }), c, ['cs_a', 'push', '5']);
    await expect(runFlow(flow, c, api)).rejects.toMatchObject({ stepUpRequired: true });
  });
});
