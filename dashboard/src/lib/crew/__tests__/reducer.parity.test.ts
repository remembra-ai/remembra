/// <reference types="node" />
// Full-state parity with the Python reference reducer (spec §13.1 "Python and
// TypeScript agree"). tests/crew/test_dashboard_reducer_parity.py runs this
// file with CREW_PARITY_OUT set, then compares every state it writes with
// `remembra.crew.reducer.reduce` on the same vector, value for value.
// Without the variable (a plain `npm test`) it checks that every state is
// plain JSON and skips the export.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { reduce } from '../reducer';
import { reducerVectors } from './vectors';

describe('reducer parity export', () => {
  it('produces a plain-JSON final state for every vector', () => {
    const out: Record<string, unknown> = {};
    for (const v of reducerVectors()) {
      const state = reduce(v.snapshot, v.frames);
      const json = JSON.parse(JSON.stringify(state));
      expect(json).toStrictEqual(state); // nothing is lost in a JSON round trip (no undefined, no functions)
      out[v.name] = json;
    }
    const target = process.env.CREW_PARITY_OUT;
    if (target) writeFileSync(target, JSON.stringify(out));
    expect(Object.keys(out).length).toBeGreaterThanOrEqual(18);
  });
});
