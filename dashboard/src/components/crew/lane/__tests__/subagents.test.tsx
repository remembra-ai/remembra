// Sub-agents (owner decision, gap analysis open question 1): a sub-agent is its own
// session linked to the session that started it. The Track nests its lane under the
// parent's lane, names the parent on it, and lists the parent's running sub-agents.
// Also: an event tombstoned by account erasure changes no state.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { applyEvent, isErased } from '../../../../lib/crew/reducer';
import type { CrewState, SessionState } from '../../../../lib/crew/types';
import { buildStrip } from '../activity';
import { CrewLane } from '../CrewLane';
import { laneDepth, laneOrder, subAgentView } from '../model';
import { NOW, claim, event, session, state } from './fixture';

const noop = () => {};

function withSubAgents(): CrewState {
  const s = state();
  const sub = (id: string, callsign: string, parent: string, over: Partial<SessionState> = {}): SessionState => ({
    ...session(id, { callsign, parent_session_id: parent, sub_agent_id: 'explore', provider: 'anthropic' }),
    presence: null,
    ...over,
  });
  return {
    ...s,
    sessions: {
      ...s.sessions,
      cs_a1: sub('cs_a1', 'cc-3', 'cs_a'),
      cs_a2: sub('cs_a2', 'cc-4', 'cs_a'),
      cs_a1x: sub('cs_a1x', 'cc-5', 'cs_a1'),
      cs_done: sub('cs_done', 'cc-6', 'cs_a', { state: 'ended', end_reason: 'parent_ended' }),
      cs_orphan: sub('cs_orphan', 'cc-7', 'cs_gone'),
    },
    claims: {
      ...s.claims,
      clm_sub: claim('clm_sub', { zone_id: 'zn_rep', holder_session_id: 'cs_a1', mode: 'exclusive' }),
    },
  };
}

describe('sub-agent lanes', () => {
  it('nests every sub-agent lane under its parent, depth first', () => {
    const s = withSubAgents();
    const live = Object.values(s.sessions).filter((x) => x.state !== 'ended');
    const order = laneOrder(live).map((x) => x.callsign);
    expect(order.indexOf('cc-3')).toBe(order.indexOf('cc-1') + 1);
    expect(order.indexOf('cc-5')).toBe(order.indexOf('cc-3') + 1); // the sub-agent's own sub-agent
    expect(order.indexOf('cc-4')).toBe(order.indexOf('cc-5') + 1);
    expect(order).toContain('cc-7'); // its parent is not shown: it keeps a place of its own
    expect(order).toHaveLength(live.length);
    expect([laneDepth(s, s.sessions.cs_a), laneDepth(s, s.sessions.cs_a1), laneDepth(s, s.sessions.cs_a1x)]).toEqual([0, 1, 2]);
    expect(laneDepth(s, s.sessions.cs_orphan)).toBe(0);
  });

  it('names the parent on a sub-agent and the running sub-agents on the parent', () => {
    const s = withSubAgents();
    expect(subAgentView(s, s.sessions.cs_a1)).toEqual({ parentLabel: 'sub-agent of cc-1', subAgents: ['cc-5'] });
    expect(subAgentView(s, s.sessions.cs_a)).toEqual({ parentLabel: null, subAgents: ['cc-3', 'cc-4'] }); // not the ended one
    expect(subAgentView(s, s.sessions.cs_orphan).parentLabel).toBe('sub-agent of an ended session');
    expect(subAgentView(s, s.sessions.cs_b)).toEqual({ parentLabel: null, subAgents: [] });
  });

  it('renders the nesting, the parent and the claim held in the sub-agent’s own name', () => {
    const s = withSubAgents();
    const lane = (sid: string) =>
      renderToStaticMarkup(
        <CrewLane
          state={s}
          session={s.sessions[sid]}
          project="yaadbooks"
          strip={buildStrip([], sid, NOW)}
          nowMs={NOW}
          canAct
          quotaSource={null}
          onRequest={noop}
        />,
      );
    const child = lane('cs_a1');
    expect(child).toContain('data-parent="cs_a"');
    expect(child).toContain('margin-left:1.5rem');
    expect(child).toContain('sub-agent of cc-1');
    expect(child).toContain('aria-label="cc-3, sub-agent of cc-1, Claude Code');
    expect(child).toContain('zone reports, exclusive'); // attributed to the sub-agent
    expect(lane('cs_a1x')).toContain('margin-left:3rem');
    const parent = lane('cs_a');
    expect(parent).toContain('sub-agents: cc-3, cc-4');
    expect(parent).not.toContain('data-parent');
    expect(parent).not.toContain('margin-left');
  });
});

describe('erased events', () => {
  it('advance the cursor and change nothing else', () => {
    const s = state();
    const erased = event('session.left', 0, {
      seq: s.last_seq + 1,
      actor: { kind: 'system', id: 'erased', callsign: null, agent_id: null, user_id: null, verified: false },
      refs: {},
      payload: { erased: true },
      summary: 'erased (account deleted)',
    });
    expect(isErased(erased)).toBe(true);
    const next = applyEvent(s, erased);
    expect(next.last_seq).toBe(s.last_seq + 1);
    expect(next.sessions).toEqual(s.sessions);
    // a real event of the same type still applies
    const real = event('session.left', 0, { seq: s.last_seq + 1, payload: { reason: 'logout' }, refs: { session_id: 'cs_a' } });
    expect(isErased(real)).toBe(false);
    expect(isErased({ ...real, payload: { erased: true, extra: 1 } })).toBe(false);
  });
});
