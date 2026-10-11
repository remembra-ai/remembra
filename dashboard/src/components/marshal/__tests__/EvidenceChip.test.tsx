// Evidence chips and the rows they point at: the outline helper over a fake
// document (it marks, clears exactly what it marked, and escapes the anchor),
// and the chip's two forms.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { EvidenceChip } from '../EvidenceChip';
import { OUTLINE_ATTR, cssEscape, outlineRefs, refSelector, scrollToRef, type OutlineTarget } from '../outline';

class FakeRow implements OutlineTarget {
  attrs = new Map<string, string>();
  setAttribute(name: string, value: string) {
    this.attrs.set(name, value);
  }
  removeAttribute(name: string) {
    this.attrs.delete(name);
  }
}

function fakeRoot(rows: Record<string, FakeRow[]>) {
  const asked: string[] = [];
  return {
    asked,
    querySelectorAll(selector: string) {
      asked.push(selector);
      return rows[selector] ?? [];
    },
  };
}

describe('outlineRefs', () => {
  it('outlines every row the anchor names, and the returned function clears exactly those', () => {
    const a = new FakeRow();
    const b = new FakeRow();
    const other = new FakeRow();
    other.setAttribute(OUTLINE_ATTR, '');
    const root = fakeRoot({ '[data-marshal-ref="agent\\:codex"]': [a, b] });
    const clear = outlineRefs('agent:codex', root);
    expect(root.asked).toEqual(['[data-marshal-ref="agent\\:codex"]']);
    expect(a.attrs.has(OUTLINE_ATTR)).toBe(true);
    expect(b.attrs.has(OUTLINE_ATTR)).toBe(true);
    clear();
    expect(a.attrs.has(OUTLINE_ATTR)).toBe(false);
    expect(b.attrs.has(OUTLINE_ATTR)).toBe(false);
    expect(other.attrs.has(OUTLINE_ATTR)).toBe(true); // not ours: untouched
  });

  it('escapes the anchor before it goes into a selector', () => {
    const root = fakeRoot({});
    outlineRefs('entry:a"] , *[x="', root)();
    expect(root.asked).toEqual(['[data-marshal-ref="entry\\:a\\"\\]\\ \\,\\ \\*\\[x\\=\\""]']);
    expect(refSelector('entry:9f', (v) => `<${v}>`)).toBe('[data-marshal-ref="<entry:9f>"]');
  });

  it('a null anchor (or no document) does nothing', () => {
    const root = fakeRoot({});
    outlineRefs(null, root)();
    outlineRefs('agent:codex', null)();
    expect(root.asked).toEqual([]);
    expect(scrollToRef(null, null)).toBe(false);
  });

  it('cssEscape follows CSS.escape where the platform has none', () => {
    expect(cssEscape('agent:claude-code')).toBe('agent\\:claude-code');
    expect(cssEscape('1abc')).toBe('\\31 abc');
    expect(cssEscape('-1a')).toBe('-\\31 a');
    expect(cssEscape('-')).toBe('\\-');
    expect(cssEscape('a\u0000b')).toBe('a�b');
    expect(cssEscape('entry:é_9')).toBe('entry\\:é_9');
  });
});

describe('EvidenceChip', () => {
  it('a read about a row is a button that can outline it', () => {
    const html = renderToStaticMarkup(<EvidenceChip n={1} evidence={{ ref: 'r1', label: 'trail/diagnosis · codex · CODEX_TRUST_MISSING · inferred', anchor: 'agent:codex' }} />);
    expect(html).toMatch(/^<button type="button"/);
    expect(html).toContain('data-anchor="agent:codex"');
    expect(html).toContain('[1] trail/diagnosis · codex · CODEX_TRUST_MISSING · inferred');
  });

  it('a read about no row is a plain chip', () => {
    const html = renderToStaticMarkup(<EvidenceChip n={2} evidence={{ ref: 'r2', label: 'cloud/plan · free · keys 2 of 3 · create_key allowed', anchor: null }} />);
    expect(html).toMatch(/^<span /);
    expect(html).not.toContain('data-anchor');
    expect(html).toContain('[2] cloud/plan · free · keys 2 of 3 · create_key allowed');
  });
});
