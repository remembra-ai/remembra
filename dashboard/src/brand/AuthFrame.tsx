// Sign-in / sign-up frame: the pixel brand panel on the left (desktop), the
// form page on the right. The form pages render unchanged inside it.

import type { ReactNode } from 'react';
import { BrandHero } from './BrandHero';

const LOG_LINES: { who: string; what: string; tone?: 'signal' }[] = [
  { who: 'claude-code', what: 'stopped · trail written' },
  { who: 'handoff', what: 'signed · 2 open, 0 failing', tone: 'signal' },
  { who: 'cursor', what: 'started · already knows' },
];

/** A retro window showing how one handoff reads on the trail (an example, not live data). */
function HandoffLog() {
  return (
    <figure className="rr-win w-[340px] max-w-full shrink-0" aria-label="Example: how a handoff reads on the trail">
      <figcaption className="rr-win-bar">
        <i aria-hidden="true" />
        handoff.log
        <span>example</span>
      </figcaption>
      <ol className="space-y-1 px-3 pb-3 pt-2.5">
        {LOG_LINES.map((line, index) => (
          <li key={line.who} className="rr-win-line flex gap-3" style={{ animationDelay: `${3.2 + index * 0.7}s` }}>
            <span className="w-[84px] shrink-0 text-ink-3">{line.who}</span>
            <span className={line.tone === 'signal' ? 'font-bold text-signal-ink' : 'text-ink'}>{line.what}</span>
          </li>
        ))}
      </ol>
    </figure>
  );
}

export function AuthFrame({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-dvh bg-paper lg:grid lg:grid-cols-[minmax(0,1.08fr)_minmax(0,1fr)]">
      <aside className="relative hidden h-screen overflow-hidden border-r border-rule bg-paper lg:sticky lg:top-0 lg:block" aria-label="About Remembra">
        <BrandHero className="absolute inset-0 block h-full w-full" />
        <div className="pointer-events-none absolute inset-x-10 top-9 flex items-center justify-between font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">
          <span>Remembra relay</span>
          <span>memory for every agent you run</span>
        </div>
        <div className="absolute inset-x-10 bottom-10 flex items-end justify-between gap-6">
          <p className="font-display min-w-0 max-w-[15ch] text-[clamp(1.6rem,1rem+1.4vw,2.4rem)] font-extrabold leading-[0.98] tracking-[-0.03em] text-ink">
            One agent stops. The next one <span className="text-signal">already knows.</span>
          </p>
          <HandoffLog />
        </div>
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}
