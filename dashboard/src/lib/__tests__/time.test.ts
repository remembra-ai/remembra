import { describe, expect, it } from 'vitest';
import { dayLabel, parseServerTime, relativeTime, where } from '../time';

describe('parseServerTime', () => {
  it('treats naive server timestamps as UTC, not local time', () => {
    expect(parseServerTime('2026-09-25T10:00:00.123456')?.toISOString()).toBe('2026-09-25T10:00:00.123Z');
    expect(parseServerTime('2026-09-25 10:00:00')?.toISOString()).toBe('2026-09-25T10:00:00.000Z');
  });

  it('keeps explicit zones', () => {
    expect(parseServerTime('2026-09-25T10:00:00+02:00')?.toISOString()).toBe('2026-09-25T08:00:00.000Z');
    expect(parseServerTime('2026-09-25T10:00:00Z')?.toISOString()).toBe('2026-09-25T10:00:00.000Z');
  });

  it('returns null for missing or invalid input', () => {
    expect(parseServerTime(null)).toBeNull();
    expect(parseServerTime('')).toBeNull();
    expect(parseServerTime('not a date')).toBeNull();
  });
});

describe('relativeTime', () => {
  const now = new Date('2026-09-25T12:00:00Z');
  it('rounds to the natural unit', () => {
    expect(relativeTime('2026-09-25T11:59:30Z', now)).toBe('just now');
    expect(relativeTime('2026-09-25T11:55:00Z', now)).toBe('5m ago');
    expect(relativeTime('2026-09-25T09:00:00Z', now)).toBe('3h ago');
    expect(relativeTime('2026-09-24T09:00:00Z', now)).toBe('yesterday');
    expect(relativeTime('2026-09-21T12:00:00Z', now)).toBe('4d ago');
  });
  it('never shows negative times for small clock drift', () => {
    expect(relativeTime('2026-09-25T12:00:20Z', now)).toBe('just now');
  });
  it('says so when the time is unknown', () => {
    expect(relativeTime(null, now)).toBe('unknown time');
  });
});

describe('dayLabel', () => {
  it('labels today and yesterday in local time', () => {
    const now = new Date(2026, 8, 25, 15, 0);
    expect(dayLabel(new Date(2026, 8, 25, 1, 0).toISOString(), now)).toBe('Today');
    expect(dayLabel(new Date(2026, 8, 24, 23, 0).toISOString(), now)).toBe('Yesterday');
    expect(dayLabel(new Date(2026, 8, 20, 12, 0).toISOString(), now)).not.toMatch(/Today|Yesterday/);
  });
});

describe('where', () => {
  it('formats branch@sha', () => {
    expect(where('main', '1d50ae3f9b2c')).toBe('main@1d50ae3');
    expect(where('main', null)).toBe('main');
    expect(where(null, 'abcdef123')).toBe('@abcdef1');
    expect(where(null, null)).toBe('');
  });
});
