// The Remembra mark and lockup, drawn from the generated geometry. Ink uses
// currentColor so the mark follows the theme; the baton is always signal.

import { useId } from 'react';
import { LOCKUP_H, MARK_BATON, MARK_BOX, MARK_INK, WORD_INK, WORD_SIG, WORD_SW } from './geometry';

const [BX0, BY0, BX1, BY1] = MARK_BOX;
const PAD = 1.5;
const MARK_VIEW = `${BX0 - PAD} ${BY0 - PAD} ${BX1 - BX0 + 2 * PAD} ${BY1 - BY0 + 2 * PAD}`;
const MARK_RATIO = (BY1 - BY0 + 2 * PAD) / (BX1 - BX0 + 2 * PAD);

/** The brain with the orange baton. `size` is the width in px. */
export function BrandMark({ size = 28, className, title }: { size?: number; className?: string; title?: string }) {
  const titleId = useId();
  return (
    <svg
      width={size}
      height={Math.round(size * MARK_RATIO * 100) / 100}
      viewBox={MARK_VIEW}
      className={className}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      aria-labelledby={title ? titleId : undefined}
    >
      {title && <title id={titleId}>{title}</title>}
      <path fill="currentColor" fillRule="evenodd" d={MARK_INK} />
      <path fill="var(--signal)" d={MARK_BATON} />
    </svg>
  );
}

/** Mark + wordmark on one line; the baton lines up with the e crossbar in "mem". */
export function BrandLockup({ height = 28, className, label = 'Remembra' }: { height?: number; className?: string; label?: string }) {
  const [x, y, w, h] = LOCKUP_H.viewBox;
  return (
    <svg
      height={height}
      width={Math.round((height * w) / h)}
      viewBox={`${x} ${y} ${w} ${h}`}
      className={className}
      role="img"
      aria-label={label}
    >
      <g transform={`translate(${LOCKUP_H.tx} ${LOCKUP_H.ty}) scale(${LOCKUP_H.k})`}>
        <path fill="currentColor" fillRule="evenodd" d={MARK_INK} />
        <path fill="var(--signal)" d={MARK_BATON} />
      </g>
      <g fill="none" stroke="currentColor" strokeWidth={WORD_SW} strokeLinecap="round" strokeLinejoin="round">
        {WORD_INK.map((d) => (
          <path key={d} d={d} />
        ))}
      </g>
      <g fill="none" stroke="var(--signal)" strokeWidth={WORD_SW} strokeLinecap="round">
        {WORD_SIG.map((d) => (
          <path key={d} d={d} />
        ))}
      </g>
    </svg>
  );
}
