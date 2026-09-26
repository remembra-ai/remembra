import { describe, expect, it } from 'vitest';
import { CrewApiError } from '../../../../lib/crew/api';
import { performZoneAction, undoTemporaryZones, type ZoneActionDeps } from '../zoneActions';

function deps(overrides: { overrideFails?: unknown; claimStatus?: string } = {}) {
  const calls: string[] = [];
  const d: ZoneActionDeps = {
    crewId: 'crw_1',
    zone: { id: 'zn_pos', slug: 'pos' },
    target: { id: 'clm_pos' },
    run: (what, fn) => {
      calls.push(`run:${what}`);
      return fn();
    },
    crewApi: {
      overrideClaim: async (id, body) => {
        calls.push(`override:${id}:${body.action}:${body.to ?? ''}:${body.reason}`);
        if (overrides.overrideFails) throw overrides.overrideFails;
        return {};
      },
      freezeZone: async (id, reason, until) => {
        calls.push(`freeze:${id}:${reason}:${until ?? ''}`);
        return {};
      },
      unfreezeZone: async (id, reason) => {
        calls.push(`unfreeze:${id}:${reason}`);
        return {};
      },
    },
    zonesApi: {
      humanClaim: async (crewId, zoneId, reason) => {
        calls.push(`claim:${crewId}:${zoneId}:${reason}`);
        const status = overrides.claimStatus ?? 'granted';
        return { status, claim: status === 'granted' ? ({ id: 'clm_h' } as never) : null };
      },
      releaseClaim: async (id, note) => {
        calls.push(`release:${id}:${note}`);
        return {};
      },
    },
  };
  return { d, calls };
}

const input = { reason: 'hand it over', sessionId: 'cs_b', until: null };

describe('zone actions', () => {
  it('grants a free zone: a human claim, then a transfer to the session', async () => {
    const { d, calls } = deps();
    await performZoneAction('grant', { ...d, target: null }, input);
    expect(calls).toEqual(['claim:crw_1:zn_pos:hand it over', 'run:Granting a zone', 'override:clm_h:transfer:cs_b:hand it over']);
  });

  it('releases the human claim when the hand-over fails, and reports the failure', async () => {
    const err = new CrewApiError('Sign in again', 401, 'step_up_required');
    const { d, calls } = deps({ overrideFails: err });
    await expect(performZoneAction('grant', { ...d, target: null }, input)).rejects.toBe(err);
    expect(calls.at(-1)).toBe('release:clm_h:grant not completed');
  });

  it('refuses to grant when the zone could not be taken first', async () => {
    const { d, calls } = deps({ claimStatus: 'queued' });
    await expect(performZoneAction('grant', { ...d, target: null }, input)).rejects.toThrow('Could not take zone pos first (queued).');
    expect(calls).toEqual(['claim:crw_1:zn_pos:hand it over']);
  });

  it('transfers, holds and revokes the live claim through the step-up runner', async () => {
    const { d, calls } = deps();
    await performZoneAction('transfer', d, input);
    await performZoneAction('hold', d, input);
    await performZoneAction('revoke', d, input);
    expect(calls).toEqual([
      'run:Transferring a zone',
      'override:clm_pos:transfer:cs_b:hand it over',
      'run:Holding a zone',
      'override:clm_pos:hold::hand it over',
      'run:Revoking a claim',
      'override:clm_pos:revoke::hand it over',
    ]);
    await expect(performZoneAction('revoke', { ...d, target: null }, input)).rejects.toThrow('Zone pos has no live claim to act on.');
    await expect(performZoneAction('transfer', d, { ...input, sessionId: null })).rejects.toThrow('Pick the session');
  });

  it('freezes (with an optional end) and unfreezes', async () => {
    const { d, calls } = deps();
    await performZoneAction('freeze', d, { reason: 'mine now', sessionId: null, until: '2026-09-26T18:00:00.000Z' });
    await performZoneAction('unfreeze', d, { reason: 'done', sessionId: null, until: null });
    expect(calls).toEqual(['run:Freezing a zone', 'freeze:zn_pos:mine now:2026-09-26T18:00:00.000Z', 'run:Unfreezing a zone', 'unfreeze:zn_pos:done']);
  });

  it('undoes only the temporary zones', async () => {
    const archived: string[] = [];
    const n = await undoTemporaryZones(
      [
        { id: 'a', source: 'suggested' },
        { id: 'b', source: 'repo' },
        { id: 'c', source: 'suggested', archived_at: '2026-01-01T00:00:00Z' },
        { id: 'd', source: 'builtin', builtin: true },
        { id: 'e', source: 'suggested' },
      ],
      {
        archiveZone: async (id) => {
          archived.push(id);
          return { applied: true };
        },
      },
      (_w, fn) => fn(),
    );
    expect(n).toBe(2);
    expect(archived).toEqual(['a', 'e']);
  });
});
