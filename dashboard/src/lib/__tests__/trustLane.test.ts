import { describe, expect, it } from 'vitest';
import { formatGap, healthBadge, healthShareLine, pickupLine, type HandoffHealth } from '../relay';
import { handoffsNote } from '../credits';

const health = (status: string, label: string, missing: string[] = []): HandoffHealth => ({
  status,
  label,
  missing,
  warnings: [],
  rules_version: 1,
});

describe('healthBadge (R-21)', () => {
  it('maps each server grade to a tone and explains what is missing', () => {
    expect(healthBadge(health('ready', 'Ready'))).toEqual({
      label: 'Ready',
      tone: 'ok',
      title: 'Ready: nothing left open in the recorded facts',
    });
    expect(healthBadge(health('ready_with_warnings', 'Ready with warnings', ['2 commit(s) not pushed', 'tests not run']))).toEqual({
      label: 'Ready with warnings',
      tone: 'open',
      title: 'Ready with warnings: 2 commit(s) not pushed; tests not run',
    });
    expect(healthBadge(health('blocked', 'Blocked', ['1 failing test run(s)']))?.tone).toBe('fail');
    expect(healthBadge(health('conflicted', 'Conflicted'))?.tone).toBe('fail');
    expect(healthBadge(health('incomplete', 'Incomplete'))?.tone).toBe('neutral');
    expect(healthBadge(health('something_new', 'New grade'))?.tone).toBe('neutral');
  });

  it('shows nothing for ungraded (legacy or free-form) handoffs', () => {
    expect(healthBadge(null)).toBeNull();
    expect(healthBadge(undefined)).toBeNull();
  });
});

describe('pickupLine (R-18)', () => {
  const name = (id: string) => ({ codex: 'Codex', cursor: 'Cursor' })[id] ?? id;

  it('says who picked the handoff up and how soon', () => {
    const at = '2026-09-26T10:00:00+00:00';
    expect(pickupLine([{ agent_id: 'codex', agent_verified: true, picked_up_at: at, gap_seconds: 120 }], name)).toBe(
      'picked up by Codex 2m after it stopped',
    );
    expect(
      pickupLine(
        [
          { agent_id: 'codex', agent_verified: false, picked_up_at: at, gap_seconds: 30 },
          { agent_id: 'cursor', agent_verified: false, picked_up_at: at, gap_seconds: 7200 },
        ],
        name,
      ),
    ).toBe('picked up by Codex under a minute after it stopped, then Cursor');
    expect(pickupLine([{ agent_id: 'gemini', agent_verified: false, picked_up_at: at, gap_seconds: null }], name)).toBe(
      'picked up by gemini',
    );
  });

  it('is empty when nobody picked it up', () => {
    expect(pickupLine([], name)).toBeNull();
    expect(pickupLine(undefined, name)).toBeNull();
  });

  it('formats gaps', () => {
    expect(formatGap(59)).toBe('under a minute');
    expect(formatGap(3600)).toBe('1h');
    expect(formatGap(3900)).toBe('1h 5m');
    expect(formatGap(3 * 86400 + 5)).toBe('3d');
    expect(formatGap(-1)).toBeNull();
    expect(formatGap(null)).toBeNull();
  });
});

describe('handoffsNote (R-17)', () => {
  it('shows session handoffs apart from the capped memories', () => {
    expect(handoffsNote({ memories: { stored: 10, cap: 10000, handoffs: 20000 } })).toBe('20,000 handoffs (not counted)');
    expect(handoffsNote({ memories: { stored: 10, cap: 10000, handoffs: 1 } })).toBe('1 handoff (not counted)');
    expect(handoffsNote({ memories: { stored: 10, cap: 10000, handoffs: 0 } })).toBe('0 handoffs (not counted)');
    expect(handoffsNote({ memories: { stored: 10, cap: 10000 } })).toBeNull(); // older server
  });
});

describe('healthShareLine (admin)', () => {
  it('lists the grades a week had', () => {
    expect(
      healthShareLine({ handoffs: 4, health_share: { ready: 0.5, ready_with_warnings: 0.25, blocked: 0.25, not_graded: 0 } }),
    ).toBe('50% ready · 25% warnings · 25% blocked');
    expect(healthShareLine({ handoffs: 0, health_share: {} })).toBe('none');
  });
});
