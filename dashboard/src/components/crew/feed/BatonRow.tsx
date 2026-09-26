// The baton row's first line: from ⇢ to along a small dashed orange bezier,
// the zones that went with it (✦) and whether the saved work came back.

import type { CrewEvent, CrewState } from '../../../lib/crew/types';
import { batonText } from './model';

export function BatonRow({ event, state }: { event: CrewEvent; state: CrewState | null }) {
  const b = batonText(event, state);
  return (
    <span className="flex min-w-0 items-center gap-1.5 truncate font-mono text-[12px] text-ink">
      <span className="shrink-0">{b.from ?? 'a stopped agent'}</span>
      <svg aria-hidden="true" viewBox="0 0 44 12" width="44" height="12" className="shrink-0 overflow-visible">
        <path d="M2 9 C 14 -1, 30 -1, 42 9" fill="none" stroke="var(--signal)" strokeWidth="2" strokeDasharray="3 3" strokeLinecap="round" />
        <rect x="38" y="7" width="4" height="4" fill="var(--signal)" />
      </svg>
      <span className="sr-only">passed the baton to</span>
      <span className="shrink-0 font-semibold">{b.to}</span>
      {b.task && <span className="shrink-0 text-ink-3">· {b.task}</span>}
      {b.zones.length > 0 && <span className="min-w-0 truncate text-signal-ink">· ✦ {b.zones.join(', ')}</span>}
      {b.restored === true && <span className="shrink-0 text-ok">· work restored ✓</span>}
      {b.restored === false && <span className="shrink-0 text-fail">· work not restored</span>}
      {b.restored === null && b.savedWork && <span className="shrink-0 text-ink-3">· saved work attached</span>}
    </span>
  );
}
