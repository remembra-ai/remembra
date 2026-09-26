import { describe, expect, it } from 'vitest';
import { applyEvent, fromSnapshot } from '../reducer';
import { describeHolder, liveSessions, needsYouCount, presenceText, sessionLabel, sortedZones, taskRef, zoneBySlug, zoneClaims } from '../selectors';
import type { ClaimView, CrewSnapshot, CrewState } from '../types';
import { reducerVectors } from './vectors';

const SNAP: CrewSnapshot = reducerVectors().find((v) => v.name === 'snapshot_init')!.snapshot;

function withClaim(state: CrewState, claim: Partial<ClaimView> & { id: string }): CrewState {
  const base = Object.values(state.claims)[0];
  return { ...state, claims: { ...state.claims, [claim.id]: { ...base, ...claim } } };
}

describe('crew selectors', () => {
  const state = fromSnapshot(SNAP);

  it('list live sessions by callsign and label them with their verification', () => {
    expect(liveSessions(state).map((s) => s.callsign)).toEqual(['cc-1', 'codex-1']);
    expect(sessionLabel(state.sessions.cs_a)).toBe('cc-1 · claude-code (key-verified)');
    expect(sessionLabel({ callsign: 'codex-2', agent_id: 'codex', agent_verified: false })).toBe('codex-2 · codex (self-declared)');
  });

  it('word presence states without relying on colour', () => {
    const s = state.sessions.cs_a;
    expect(presenceText(s)).toBe('active');
    expect(presenceText({ ...s, state: 'quiet', quiet_reason: 'host_unreachable' })).toBe('quiet (host offline)');
    expect(presenceText({ ...s, state: 'quiet', quiet_reason: 'mcp_silent' })).toBe('quiet (no MCP calls)');
    expect(presenceText({ ...s, state: 'quota_blocked', state_reason: 'billing_error' })).toBe('stopped (billing_error)');
    expect(presenceText({ ...s, state: 'idle', stuck: true })).toBe('idle · stuck');
  });

  it('describe who holds a zone', () => {
    const pos = zoneBySlug(state, 'pos')!;
    expect(describeHolder(state, pos)).toBe('held EXCLUSIVELY by cc-1 for T-14 · active');
    const reports = zoneBySlug(state, 'reports')!;
    expect(describeHolder(state, reports)).toBe('free');

    const reserved = withClaim(state, { id: 'clm_pos', state: 'reserved', reserve_reason: 'quota' });
    expect(describeHolder(reserved, pos)).toBe('RESERVED for the next pickup of T-14 (quota)');

    const queued = withClaim(state, { id: 'clm_q', state: 'queued', holder_session_id: 'cs_b', queue_pos: 1 });
    expect(describeHolder(queued, pos)).toBe('held EXCLUSIVELY by cc-1 for T-14 · active · 1 waiting');
    expect(zoneClaims(queued, pos.id).map((c) => c.id)).toEqual(['clm_pos', 'clm_q']);

    const frozen = {
      ...state,
      zones: { ...state.zones, [reports.id]: { ...reports, frozen_by: 'u_mani' } },
    };
    expect(describeHolder(frozen, frozen.zones[reports.id])).toBe('frozen by a human');
  });

  it('resolve task refs and counts', () => {
    expect(taskRef(state, 'tsk_14')).toBe('T-14');
    expect(taskRef(state, 'tsk_unknown')).toBe('tsk_unknown');
    expect(taskRef(state, null)).toBeNull();
    expect(needsYouCount(state)).toBe(SNAP.inbox_counts.project);
    expect(sortedZones(state).map((z) => z.slug)).toEqual(['pos', 'reports']);
  });

  it('drop ended sessions from the live list', () => {
    const next = applyEvent(state, {
      seq: state.last_seq + 1,
      crew_id: SNAP.crew.id,
      type: 'session.left',
      refs: { session_id: 'cs_b' },
      payload: { reason: 'done' },
    });
    expect(liveSessions(next).map((s) => s.id)).toEqual(['cs_a']);
  });
});
