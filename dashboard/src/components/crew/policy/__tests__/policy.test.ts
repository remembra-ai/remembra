import { describe, expect, it } from 'vitest';
import { CrewApiError } from '../../../../lib/crew/api';
import { crewState } from '../../zones/__tests__/fixtures';
import { actorAccess, liveWords } from '../../zones/live';
import {
  ENFORCEMENT_CHOICES,
  TRUTH_TABLE,
  bypassUsage,
  checkoutRows,
  codeTimeLeft,
  isLowering,
  truthRowFor,
} from '../policyModel';
import { actionErrorText, needsStepUp, reauthenticate, signedInUser } from '../stepUp';

describe('policy model', () => {
  it('knows which enforcement changes lower protection (human-only, announced)', () => {
    expect(ENFORCEMENT_CHOICES.map((c) => c.value)).toEqual(['enforce', 'observe', 'off']);
    expect(isLowering('enforce', 'observe')).toBe(true);
    expect(isLowering('observe', 'off')).toBe(true);
    expect(isLowering('off', 'enforce')).toBe(false);
    expect(isLowering('enforce', 'enforce')).toBe(false);
  });

  it('groups live sessions by checkout, worst git-gate state first', () => {
    const rows = checkoutRows(crewState());
    expect(rows.map((r) => [r.worktreeId, r.hook, r.sessions.map((s) => s.callsign)])).toEqual([
      ['wt-b', 'missing', ['cc-2', 'codex-1']],
      ['wt-cc-1', 'ok', ['cc-1']],
    ]);
    expect(rows[0].branches).toEqual(['main']);
  });

  it('counts a bypass code down by the server clock', () => {
    const now = Date.parse('2026-09-26T14:00:00Z');
    expect(codeTimeLeft('2026-09-26T14:12:04Z', now)).toBe('12:04 left');
    expect(codeTimeLeft('2026-09-26T14:12:04Z', now, 60_000)).toBe('11:04 left'); // server is a minute ahead
    expect(codeTimeLeft('2026-09-26T13:59:00Z', now)).toBeNull();
    expect(codeTimeLeft('garbage', now)).toBeNull();
    expect(bypassUsage('push', 'RCB-AAAAA-BBBBB')).toBe('REMEMBRA_BYPASS=RCB-AAAAA-BBBBB git push');
    expect(bypassUsage('commit', 'RCB-AAAAA-BBBBB')).toBe('REMEMBRA_BYPASS=RCB-AAAAA-BBBBB git commit');
    expect(bypassUsage('write:pos', 'RCB-AAAAA-BBBBB')).toBe('REMEMBRA_BYPASS=RCB-AAAAA-BBBBB');
  });

  it('places each live session in the enforcement truth table', () => {
    const s = crewState().sessions;
    expect(TRUTH_TABLE).toHaveLength(6);
    expect(truthRowFor(s.cs_a)).toBe(0); // Claude Code
    expect(truthRowFor(s.cs_b)).toBe(2); // Codex, advisory → fence
    expect(truthRowFor({ ...s.cs_b, adapter_enforcement: 'enforced' })).toBe(1);
    expect(truthRowFor({ ...s.cs_b, agent_id: 'cursor', adapter: 'cursor' })).toBe(3);
    expect(truthRowFor({ ...s.cs_a, client_kind: 'mcp' })).toBe(4);
  });
});

describe('access and live words', () => {
  it('lets only a dashboard login with an owner/admin role act (D27)', () => {
    expect(actorAccess(undefined)).toEqual({ canAct: false, why: 'Checking your access…' });
    expect(actorAccess({ human: false, role: 'owner', permissions: ['crew:read', 'crew:write'] }).why).toMatch(/API keys never can/);
    expect(actorAccess({ human: true, role: 'member', permissions: ['crew:read', 'crew:write', 'crew:claim'] }).why).toMatch(/role \(member\)/);
    expect(actorAccess({ human: true, role: 'owner', permissions: ['crew:read', 'crew:override', 'crew:admin'] })).toEqual({ canAct: true, why: null });
  });

  it('words the stream status', () => {
    expect(liveWords('live', 'open')).toEqual({ text: 'live', live: true });
    expect(liveWords('polling', 'closed')).toEqual({ text: 'updating every few seconds', live: false });
    expect(liveWords('polling', 'unauthorized').text).toBe('signed out: reload to sign in');
    expect(liveWords('not_found', 'open').text).toBe('not found');
  });
});

describe('step-up', () => {
  it('reads the signed-in account, or null for an API-key session', () => {
    const store = (v: string | null) => ({ getItem: () => v });
    expect(signedInUser(store(JSON.stringify({ id: 'u1', email: 'mani@example.com' })))).toEqual({ id: 'u1', email: 'mani@example.com' });
    expect(signedInUser(store(null))).toBeNull();
    expect(signedInUser(store('{bad json'))).toBeNull();
    expect(signedInUser(store(JSON.stringify({ id: 'u1' })))).toBeNull();
    expect(signedInUser(null)).toBeNull();
  });

  it('signs in again and returns the fresh token, or says why not', async () => {
    const calls: { url: string; body: unknown }[] = [];
    const ok = async (url: string, init?: RequestInit) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify({ access_token: 'jwt-new' }), { status: 200 });
    };
    await expect(reauthenticate(ok, '/api/v1', 'mani@example.com', 'pw')).resolves.toBe('jwt-new');
    expect(calls).toEqual([{ url: '/api/v1/auth/login', body: { email: 'mani@example.com', password: 'pw' } }]);
    const wrong = async () => new Response(JSON.stringify({ detail: 'Invalid email or password' }), { status: 401 });
    await expect(reauthenticate(wrong, '/api/v1', 'm', 'x')).rejects.toThrow('That password did not match this account.');
    const down = async () => {
      throw new TypeError('network');
    };
    await expect(reauthenticate(down, '/api/v1', 'm', 'x')).rejects.toThrow('Could not reach the Remembra server.');
    const locked = async () => new Response(JSON.stringify({ detail: 'Account locked' }), { status: 423 });
    await expect(reauthenticate(locked, '/api/v1', 'm', 'x')).rejects.toThrow('Account locked');
  });

  it('explains failed actions in plain words', () => {
    const stepUp = new CrewApiError('x', 401, 'step_up_required');
    expect(needsStepUp(stepUp)).toBe(true);
    expect(needsStepUp(new CrewApiError('x', 401, 'not_authenticated'))).toBe(false);
    expect(actionErrorText(stepUp)).toMatch(/Sign in again/);
    expect(actionErrorText(new CrewApiError('x', 403, 'human_only'))).toMatch(/API keys never can/);
    expect(actionErrorText(new CrewApiError('x', 404, 'not_found'))).toMatch(/Not found/);
    expect(actionErrorText(new CrewApiError('x', 412, 'version_mismatch'))).toMatch(/changed while you were looking/);
    expect(actionErrorText(new CrewApiError('Zone pos is frozen by a human.', 423, 'frozen'))).toBe('Zone pos is frozen by a human.');
    expect(actionErrorText(new Error('boom'))).toBe('boom');
  });
});
