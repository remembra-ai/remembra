import { useEffect, useRef } from 'react';
import clsx from 'clsx';
import type { EvidenceRef } from '../../lib/marshalDesk';
import { outlineRefs, scrollToRef } from './outline';

const CHIP =
  'inline-block max-w-full truncate rounded-[2px] border border-rule px-1.5 py-1 align-top font-mono text-[11px] leading-tight text-ink-2 sm:py-0.5';

/**
 * `[n] <label>`: a read the call rests on. Hover or focus outlines the row it
 * is about on the page (when that row is on screen); a click scrolls to it.
 * A read about no row is a plain chip.
 */
export function EvidenceChip({ n, evidence }: { n: number; evidence: EvidenceRef }) {
  const clear = useRef<(() => void) | null>(null);
  useEffect(
    () => () => {
      clear.current?.();
      clear.current = null;
    },
    [],
  );
  const text = `[${n}] ${evidence.label}`;
  const anchor = evidence.anchor;
  if (!anchor) {
    return (
      <span className={CHIP} title={evidence.label}>
        {text}
      </span>
    );
  }
  const show = () => {
    clear.current?.();
    clear.current = outlineRefs(anchor, document);
  };
  const hide = () => {
    clear.current?.();
    clear.current = null;
  };
  return (
    <button
      type="button"
      className={clsx(CHIP, 'text-left hover:border-ink hover:text-ink')}
      title={evidence.label}
      data-anchor={anchor}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      onClick={() => scrollToRef(anchor)}
    >
      {text}
    </button>
  );
}
