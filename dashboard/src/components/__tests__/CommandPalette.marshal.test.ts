// The palette's `?` mode (spec 4.5): `?why is codex waiting` becomes one item
// that opens the Marshal desk and asks it; a bare `?` opens the desk; nothing
// changes for an account without the desk. The palette wires this helper to
// desk.open, and its Actions list gains "Ask Marshal" the same way.

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { countChars, marshalPaletteItem } from '../../lib/marshalDesk';

const ON = { available: true, optedOut: false };

describe('marshalPaletteItem', () => {
  it('?question asks it', () => {
    expect(marshalPaletteItem('?why is codex waiting', ON)).toEqual({
      label: 'Ask Marshal: "why is codex waiting"',
      question: 'why is codex waiting',
      ask: true,
    });
    expect(marshalPaletteItem('  ?   what did claude-code hand off last  ', ON)?.question).toBe('what did claude-code hand off last');
  });

  it('a bare ? opens the desk without asking', () => {
    expect(marshalPaletteItem('?', ON)).toEqual({ label: 'Ask Marshal', question: '', ask: false });
  });

  it('is off without the desk, when opted out, and for a query that is not a ? question', () => {
    expect(marshalPaletteItem('?why', { available: false, optedOut: false })).toBeNull();
    expect(marshalPaletteItem('?why', { available: true, optedOut: true })).toBeNull();
    expect(marshalPaletteItem('why ?', ON)).toBeNull();
    expect(marshalPaletteItem('', ON)).toBeNull();
  });

  it('clips a question past 1,000 code points', () => {
    const item = marshalPaletteItem(`?${'é'.repeat(1500)}`, ON);
    expect(countChars(item?.question ?? '')).toBe(1000);
    expect(item?.ask).toBe(true);
  });
});

describe('the palette wiring', () => {
  const source = readFileSync(new URL('../CommandPalette.tsx', import.meta.url), 'utf8');

  it('opens the desk from the ? item and from Actions, then closes itself', () => {
    expect(source).toMatch(/marshalPaletteItem\(query, desk\)/);
    expect(source).toMatch(/desk\.open\(\{ question: marshalItem\.question \|\| undefined, ask: marshalItem\.ask, source: 'palette' \}\);\s*onClose\(\);/);
    expect(source).toMatch(/id: 'ask-marshal', label: 'Ask Marshal'[^\n]*keywords: \['marshal', 'why', 'doctor'\]/);
  });

  it('the trail mark replaces the sparkle', () => {
    expect(source).not.toMatch(/\bSparkles\b/);
    expect(source).toMatch(/mode === 'search' \? \(\s*<MarshalIcon/);
  });
});
