// Evidence chips point at rows on the page: HomeCards' agent rows carry
// data-marshal-ref="agent:<id>" and trail nodes data-marshal-ref="entry:<id>".
// Hovering or focusing a chip outlines every matching row (index.css draws
// [data-marshal-outline]); a click scrolls to the first one.

/** What the outline needs from an element (a real Element, or a test double). */
export interface OutlineTarget {
  setAttribute(name: string, value: string): void;
  removeAttribute(name: string): void;
}

export interface OutlineRoot {
  querySelectorAll(selector: string): ArrayLike<OutlineTarget> | Iterable<OutlineTarget>;
}

export const OUTLINE_ATTR = 'data-marshal-outline';

/**
 * CSS.escape (CSSOM §2.1.1), with the platform's own when there is one. An
 * anchor is built from ids the server read, so it is escaped before it goes
 * into a selector.
 */
export function cssEscape(value: string): string {
  const native = (globalThis as { CSS?: { escape?: (v: string) => string } }).CSS?.escape;
  if (native) return native(value);
  let out = '';
  const first = value.charCodeAt(0);
  for (let i = 0; i < value.length; i += 1) {
    const c = value.charCodeAt(i);
    if (c === 0x0000) out += '�';
    else if ((c >= 0x0001 && c <= 0x001f) || c === 0x007f || (i === 0 && c >= 0x30 && c <= 0x39) || (i === 1 && c >= 0x30 && c <= 0x39 && first === 0x2d)) {
      out += `\\${c.toString(16)} `;
    } else if (i === 0 && value.length === 1 && c === 0x2d) out += `\\${value[i]}`;
    else if (c >= 0x80 || c === 0x2d || c === 0x5f || (c >= 0x30 && c <= 0x39) || (c >= 0x41 && c <= 0x5a) || (c >= 0x61 && c <= 0x7a)) out += value[i];
    else out += `\\${value[i]}`;
  }
  return out;
}

export function refSelector(anchor: string, escape: (value: string) => string = cssEscape): string {
  return `[data-marshal-ref="${escape(anchor)}"]`;
}

/** Outline every row `anchor` names; the returned function clears exactly those. A null anchor does nothing. */
export function outlineRefs(
  anchor: string | null,
  root: OutlineRoot | null | undefined,
  escape: (value: string) => string = cssEscape,
): () => void {
  if (!anchor || !root) return () => {};
  const targets = Array.from(root.querySelectorAll(refSelector(anchor, escape)));
  for (const el of targets) el.setAttribute(OUTLINE_ATTR, '');
  return () => {
    for (const el of targets) el.removeAttribute(OUTLINE_ATTR);
  };
}

/** Bring the first row `anchor` names into view (no travel under reduced motion). */
export function scrollToRef(anchor: string | null, root: ParentNode | null | undefined = typeof document === 'undefined' ? null : document): boolean {
  if (!anchor || !root) return false;
  const el = root.querySelector(refSelector(anchor));
  if (!el) return false;
  const still = typeof window !== 'undefined' && !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({ block: 'center', behavior: still ? 'auto' : 'smooth' });
  return true;
}
