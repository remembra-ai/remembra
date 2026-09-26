// Loads the shared crew contract vectors (tests/crew/vectors, WP-0a) and
// implements the reducer assertion language from docs/crew/reducer.md.

import type { CrewSnapshot } from '../types';
import type { FrameLike } from '../reducer';

export interface ReducerAssertion {
  path: (string | number)[];
  equals?: unknown;
  absent?: boolean;
  length?: number;
}

export interface ReducerVector {
  name: string;
  description: string;
  snapshot: CrewSnapshot;
  frames: FrameLike[];
  expect: ReducerAssertion[];
  events_validate?: boolean;
}

const reducerFiles = import.meta.glob('../../../../../tests/crew/vectors/reducer/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, ReducerVector>;

const sampleFiles = import.meta.glob('../../../../../tests/crew/vectors/events/samples.json', {
  eager: true,
  import: 'default',
}) as Record<string, { valid: { type: string }[] }>;

/** Every reducer vector, sorted by file name (the Python loader's order). Fresh copies. */
export function reducerVectors(): ReducerVector[] {
  return Object.keys(reducerFiles)
    .sort()
    .map((key) => JSON.parse(JSON.stringify(reducerFiles[key])) as ReducerVector);
}

/** One valid envelope per L0 event type (events/samples.json). */
export function eventSamples(): { type: string }[] {
  const files = Object.values(sampleFiles);
  if (files.length !== 1) throw new Error('events/samples.json not found');
  return JSON.parse(JSON.stringify(files[0].valid));
}

const MISSING = Symbol('missing');

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function getPath(state: unknown, path: (string | number)[]): unknown {
  let cur: unknown = state;
  for (const seg of path) {
    if (typeof seg === 'number') {
      if (!Array.isArray(cur) || seg >= cur.length) return MISSING;
      cur = cur[seg];
    } else {
      if (!isPlainObject(cur) || !Object.prototype.hasOwnProperty.call(cur, seg)) return MISSING;
      cur = cur[seg];
    }
  }
  return cur;
}

function jsonType(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

/** Deep JSON equality where the JSON type must match at every level (`1` ≠ `true`, `[]` ≠ `{}`). */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (jsonType(a) !== jsonType(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => jsonEqual(v, b[i]));
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && jsonEqual(a[k], b[k]));
  }
  return a === b;
}

/** Readable failures for the assertions (empty = the state satisfies the vector). */
export function checkAssertions(state: unknown, expect: ReducerAssertion[]): string[] {
  const failures: string[] = [];
  for (const a of expect) {
    const value = getPath(state, a.path);
    const where = a.path.join('.') || '<root>';
    if (a.absent) {
      if (value !== MISSING) failures.push(`${where}: expected absent, got ${JSON.stringify(value)}`);
    } else if (a.length !== undefined) {
      const len = Array.isArray(value) ? value.length : isPlainObject(value) ? Object.keys(value).length : typeof value === 'string' ? value.length : undefined;
      if (len !== a.length) failures.push(`${where}: expected length ${a.length}, got ${value === MISSING ? 'missing' : len}`);
    } else if (value === MISSING) {
      failures.push(`${where}: missing (expected ${JSON.stringify(a.equals)})`);
    } else if (!jsonEqual(value, a.equals)) {
      failures.push(`${where}: expected ${JSON.stringify(a.equals)}, got ${JSON.stringify(value)}`);
    }
  }
  return failures;
}

/** Run every vector through `reduceFn`; returns {vector name: failures} for the failing ones. */
export function runReducerVectors(
  reduceFn: (snapshot: CrewSnapshot, frames: FrameLike[]) => unknown,
): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const v of reducerVectors()) {
    const state = JSON.parse(JSON.stringify(reduceFn(v.snapshot, v.frames)));
    const failures = checkAssertions(state, v.expect);
    if (failures.length) out[v.name] = failures;
  }
  return out;
}
