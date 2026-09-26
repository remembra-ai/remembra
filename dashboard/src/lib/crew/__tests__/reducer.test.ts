import { describe, expect, it } from 'vitest';
import {
  BATON_REFS_KEEP,
  HANDLED_EVENT_TYPES,
  MESSAGES_KEEP,
  applyEvent,
  applyFrame,
  fromSnapshot,
  reduce,
  type FrameLike,
} from '../reducer';
import type { CrewSnapshot, CrewState } from '../types';
import { checkAssertions, eventSamples, reducerVectors, runReducerVectors } from './vectors';

const VECTORS = reducerVectors();

describe('shared reducer vectors (tests/crew/vectors/reducer, WP-0a)', () => {
  it('loads every vector and each has assertions', () => {
    expect(VECTORS.length).toBeGreaterThanOrEqual(18);
    for (const v of VECTORS) {
      expect(v.expect.length, v.name).toBeGreaterThan(0);
      expect(v.description, v.name).toBeTruthy();
    }
  });

  it.each(VECTORS.map((v) => [v.name, v] as const))('satisfies %s', (_name, vector) => {
    const state = JSON.parse(JSON.stringify(reduce(vector.snapshot, vector.frames)));
    expect(checkAssertions(state, vector.expect)).toEqual([]);
  });

  it('the runner reports nothing for this reducer', () => {
    expect(runReducerVectors(reduce)).toEqual({});
  });

  it('the runner catches a reducer that ignores events', () => {
    const failures = runReducerVectors((snap) => fromSnapshot(snap));
    expect(Object.keys(failures).length).toBeGreaterThanOrEqual(VECTORS.length - 2);
  });

  it('the runner catches a reducer that re-applies duplicates', () => {
    const sloppy = (snap: CrewSnapshot, frames: FrameLike[]) => {
      let state = fromSnapshot(snap);
      for (const f of frames) {
        if (f.type === 'crew.event' && (f.data as { seq: number }).seq <= state.last_seq) {
          state = { ...state, last_seq: (f.data as { seq: number }).seq - 1 };
        }
        state = applyFrame(state, f);
      }
      return state;
    };
    expect(Object.keys(runReducerVectors(sloppy))).toContain('duplicates_and_old_seq_ignored');
  });

  it('the runner catches a reducer that lets presence change state', () => {
    const leaky = (snap: CrewSnapshot, frames: FrameLike[]) => {
      let state = fromSnapshot(snap);
      for (const f of frames) {
        state = applyFrame(state, f);
        if (f.type === 'presence' && state.crew && f.crew_id === state.crew.id) {
          for (const lane of f.lanes ?? []) {
            const s = state.sessions[lane.session_id];
            if (s) state = { ...state, sessions: { ...state.sessions, [lane.session_id]: { ...s, state: lane.state } } };
          }
        }
      }
      return state;
    };
    expect(Object.keys(runReducerVectors(leaky))).toContain('presence_overlay');
  });
});

describe('reducer behaviour beyond the vectors', () => {
  it('handles exactly the L0 closed event set (events/samples.json)', () => {
    const l0 = new Set(eventSamples().map((e) => e.type));
    expect(new Set(HANDLED_EVENT_TYPES)).toEqual(l0);
  });

  it('is pure: never mutates its inputs and gives equal results twice', () => {
    for (const v of VECTORS) {
      const before = JSON.stringify(v);
      const a = reduce(v.snapshot, v.frames);
      const b = reduce(v.snapshot, v.frames);
      expect(JSON.stringify(a)).toBe(JSON.stringify(b));
      expect(JSON.stringify(v), v.name).toBe(before);
    }
  });

  it('never mutates a previous state (copy on write)', () => {
    for (const v of VECTORS) {
      let state = fromSnapshot(v.snapshot);
      for (const frame of v.frames) {
        const frozen = JSON.stringify(state);
        const next = applyFrame(state, frame);
        expect(JSON.stringify(state), `${v.name}: ${frame.type}`).toBe(frozen);
        state = next;
      }
    }
  });

  it('returns the same object for frames that change nothing', () => {
    const state = fromSnapshot(VECTORS[0].snapshot);
    for (const frame of [
      { type: 'crew.subscribed', crew_id: 'x', since_seq: 0, replayed: 0 },
      { type: 'crew.summary', crews: [] },
      { type: 'whatever' },
      { type: 'presence', crew_id: 'crw_other', lanes: [] },
      { type: 'resync_required', crew_id: 'crw_other', reason: 'overflow', last_seq: 0 },
    ] as FrameLike[]) {
      expect(applyFrame(state, frame)).toBe(state);
    }
  });

  it('a snapshot frame returns a fresh state and clears resync', () => {
    const v = VECTORS.find((x) => x.name === 'gap_sets_resync');
    expect(v).toBeDefined();
    const state = reduce(v!.snapshot, v!.frames);
    expect(state.needs_resync).toBe(true);
    const fresh = applyFrame(state, { type: 'snapshot', data: v!.snapshot });
    expect(fresh).not.toBe(state);
    expect(fresh.needs_resync).toBe(false);
    expect(fresh.last_seq).toBe(v!.snapshot.as_of_seq);
  });

  it('caps baton refs, dropping the lowest seq', () => {
    let state: CrewState = fromSnapshot(VECTORS[0].snapshot);
    const crewId = state.crew!.id;
    const start = state.last_seq + 1;
    for (let i = 0; i < BATON_REFS_KEEP + 5; i++) {
      state = applyEvent(state, {
        seq: start + i,
        crew_id: crewId,
        type: 'baton.ref_created',
        v: 1,
        moment: false,
        refs: { session_id: 'cs_a' },
        actor: { kind: 'session', id: 'cs_a' },
        payload: { ref: `refs/remembra/baton/T-1/${i}`, task_id: null, dirty_files: 1, unpushed: 0 },
      });
    }
    expect(Object.keys(state.baton_refs)).toHaveLength(BATON_REFS_KEEP);
    expect(state.baton_refs['refs/remembra/baton/T-1/0']).toBeUndefined();
    expect(state.baton_refs[`refs/remembra/baton/T-1/${BATON_REFS_KEEP + 4}`]).toBeDefined();
  });

  it('keeps the last messages by seq even when they arrive out of order within the cap', () => {
    let state: CrewState = fromSnapshot(VECTORS[0].snapshot);
    const crewId = state.crew!.id;
    const start = state.last_seq + 1;
    for (let i = 0; i < MESSAGES_KEEP + 3; i++) {
      state = applyEvent(state, {
        seq: start + i,
        crew_id: crewId,
        type: 'message.posted',
        v: 1,
        payload: { message: { id: `msg_${i}`, seq: start + i, body: String(i) } },
      });
    }
    expect(state.messages).toHaveLength(MESSAGES_KEEP);
    expect(state.messages[0].id).toBe('msg_3');
    expect(state.messages.at(-1)!.id).toBe(`msg_${MESSAGES_KEEP + 2}`);
  });

  it('treats a missing or null payload as empty and still advances seq', () => {
    const state = fromSnapshot(VECTORS[0].snapshot);
    const next = applyEvent(state, { seq: state.last_seq + 1, crew_id: state.crew!.id, type: 'handoff.created', payload: null });
    expect(next.last_seq).toBe(state.last_seq + 1);
    expect(next.crew!.last_seq).toBe(state.last_seq + 1);
  });

  it('does not treat inherited object keys as event types', () => {
    const state = fromSnapshot(VECTORS[0].snapshot);
    const next = applyEvent(state, { seq: state.last_seq + 1, crew_id: state.crew!.id, type: 'constructor', payload: {} });
    expect(next.last_seq).toBe(state.last_seq + 1);
  });
});
