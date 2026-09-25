import { describe, expect, it } from 'vitest';
import { WITHDRAWN_NOTE, inboxCounts, isWithdrawn, mergeTrailPages, type InboxSummary, type TrailItem } from '../relay';
import { INSTALL_COMMAND, PIPX_INSTALL, agentState, saveKeyCommand } from '../agents';

function summary(agents: [string, number][]): InboxSummary {
  return {
    unread_total: agents.reduce((sum, [, unread]) => sum + unread, 0),
    open_total: 0,
    agents: agents.map(([agent_id, unread]) => ({ agent_id, unread, open: unread, received: unread, sent: 0, last_at: null })),
  };
}

function entry(id: string, created_at: string, extra: Partial<TrailItem> = {}): TrailItem {
  return {
    id,
    project_id: 'widget',
    memory_type: 'handoff',
    agent_id: 'claude-code',
    session_id: id,
    created_at,
    branch: null,
    head_commit: null,
    headline: id,
    failing: 0,
    open: 0,
    ...extra,
  };
}

describe('inboxCounts', () => {
  it('counts only messages to the user as theirs to read', () => {
    // The review's repro: one note from the dashboard to Codex, nothing for the user.
    expect(inboxCounts(summary([['codex', 1], ['dashboard', 0]]))).toEqual({ forYou: 0, pendingForAgents: 1, agentsWaiting: 1 });
    expect(inboxCounts(summary([['dashboard', 2], ['codex', 1], ['claude-code', 3], ['gemini', 0]]))).toEqual({
      forYou: 2,
      pendingForAgents: 4,
      agentsWaiting: 2,
    });
  });

  it('is all zeros before the summary loads', () => {
    expect(inboxCounts(undefined)).toEqual({ forYou: 0, pendingForAgents: 0, agentsWaiting: 0 });
    expect(inboxCounts(null)).toEqual({ forYou: 0, pendingForAgents: 0, agentsWaiting: 0 });
  });
});

describe('isWithdrawn', () => {
  it('recognises only the dashboard withdraw note on a done message', () => {
    expect(isWithdrawn({ status: 'done', ack_note: WITHDRAWN_NOTE })).toBe(true);
    expect(isWithdrawn({ status: 'done', ack_note: 'fixed in 1d50ae3' })).toBe(false);
    expect(isWithdrawn({ status: 'unread', ack_note: null })).toBe(false);
  });
});

describe('mergeTrailPages', () => {
  it('keeps entries that new arrivals pushed out of the head', () => {
    const before = [entry('e3', '2026-09-01T12:03:00'), entry('e2', '2026-09-01T12:02:00')];
    const olderPage = [entry('e1', '2026-09-01T12:01:00'), entry('e0', '2026-09-01T12:00:00')];
    const olderState = [...before, ...olderPage]; // what Trail stores when "Older entries" loads
    // Two handoffs arrive; the polled head (limit 2) no longer holds e3 / e2.
    const head = [entry('n1', '2026-09-01T12:11:00'), entry('n0', '2026-09-01T12:10:00')];
    expect(mergeTrailPages(head, olderState).map((i) => i.id)).toEqual(['n1', 'n0', 'e3', 'e2', 'e1', 'e0']);
  });

  it('drops duplicates and superseded handoffs of a re-closed session', () => {
    const head = [entry('v2', '2026-09-01T12:05:00', { session_id: 's1' }), entry('e2', '2026-09-01T12:02:00')];
    const older = [entry('e2', '2026-09-01T12:02:00'), entry('v1', '2026-09-01T12:01:00', { session_id: 's1' })];
    expect(mergeTrailPages(head, older).map((i) => i.id)).toEqual(['v2', 'e2']);
    // Checkpoints of the same session, and other agents' sessions with the same id, stay.
    const mixed = [
      entry('c1', '2026-09-01T12:04:00', { session_id: 's1', memory_type: 'checkpoint' }),
      entry('x1', '2026-09-01T12:03:00', { session_id: 's1', agent_id: 'codex' }),
    ];
    expect(mergeTrailPages(head, mixed).map((i) => i.id)).toEqual(['v2', 'e2', 'c1', 'x1']);
  });
});

describe('agentState', () => {
  const now = new Date('2026-09-25T12:00:00Z');

  it('never says working for an agent whose newest entry is a handoff', () => {
    const handoff = entry('h', '2026-09-25T11:41:00Z');
    expect(agentState('2026-09-25T11:41:00Z', handoff, now)).toEqual({ kind: 'handed-off', at: '2026-09-25T11:41:00Z' });
  });

  it('says working only for a recent checkpoint with no later handoff', () => {
    const checkpoint = entry('c', '2026-09-25T11:50:00Z', { memory_type: 'checkpoint' });
    expect(agentState('2026-09-25T11:50:00Z', checkpoint, now)).toEqual({ kind: 'working', at: '2026-09-25T11:50:00Z' });
    const stale = entry('c', '2026-09-25T09:00:00Z', { memory_type: 'checkpoint' });
    expect(agentState('2026-09-25T09:00:00Z', stale, now)).toEqual({ kind: 'idle' });
  });

  it('falls back to "active in the last hour" when the newest entry is not on the page', () => {
    expect(agentState('2026-09-25T11:30:00Z', undefined, now)).toEqual({ kind: 'recent' });
    expect(agentState(null, undefined, now)).toEqual({ kind: 'idle' });
  });
});

describe('install commands', () => {
  it('pins the first release with remembra-relay', () => {
    expect(PIPX_INSTALL).toBe("pipx install --force 'remembra>=0.16'");
    expect(INSTALL_COMMAND).toBe("pipx install --force 'remembra>=0.16' && remembra-relay connect");
  });

  it('prefills the server URL and leaves the key for the user', () => {
    expect(saveKeyCommand('https://api.example.test')).toBe(
      'remembra-install --all --api-key <your-key> --url https://api.example.test',
    );
    expect(saveKeyCommand('')).toContain('--url https://api.remembra.dev');
  });
});
