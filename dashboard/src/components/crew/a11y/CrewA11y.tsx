// Accessibility plumbing for every crew screen (§9.16):
// - CrewLiveRegions: the one polite and the one assertive live region;
// - CrewMomentAnnouncer: announces a crew's moments as they arrive (never
//   presence, never the history that was already there on open);
// - useCrewGoKeys: `g c/z/b/h/f` between the views of the crew on screen.

import { useEffect, useRef, useSyncExternalStore } from 'react';
import { useCrewSocket } from '../../../hooks/useCrewSocket';
import { crewsHref, goToCrew } from '../../../lib/crew/routes';
import { crewAnnouncer, momentAnnouncements, type Announcer } from './announcer';
import { crewGoTarget, isTypingTarget } from './keys';

export function CrewLiveRegions({ announcer = crewAnnouncer }: { announcer?: Announcer }) {
  const text = useSyncExternalStore(announcer.subscribe, announcer.getText, announcer.getText);
  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true" data-crew-live="polite">
        {text.polite}
      </div>
      <div className="sr-only" role="alert" aria-live="assertive" aria-atomic="true" data-crew-live="assertive">
        {text.assertive}
      </div>
    </>
  );
}

export function CrewMomentAnnouncer({ crewId, announcer = crewAnnouncer }: { crewId: string; announcer?: Announcer }) {
  const crew = useCrewSocket(crewId);
  const seen = useRef<number | null>(null);
  const moments = crew.state?.moments;
  useEffect(() => {
    if (!moments) return;
    const newest = moments.reduce((max, m) => Math.max(max, m.seq), 0);
    if (seen.current === null) {
      seen.current = newest; // what was there on open is history, not news
      return;
    }
    for (const a of momentAnnouncements(moments, seen.current)) announcer.announce(a.text, a.level);
    seen.current = Math.max(seen.current, newest);
  }, [moments, announcer]);
  return null;
}

/** `g` + c/z/b/h/f for the crew on screen. Runs before the global shortcuts so `g h` means the crew's track here. */
function useCrewGoKeys(project: string | null): void {
  const projectRef = useRef(project);
  useEffect(() => {
    projectRef.current = project;
  });
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const key = e.key.toLowerCase();
      if (pendingG && Date.now() - pendingG < 1200) {
        pendingG = 0;
        const target = crewGoTarget(key, projectRef.current);
        if (!target) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        if (target.kind === 'crews') window.location.hash = crewsHref();
        else goToCrew(projectRef.current!, target.screen);
        return;
      }
      if (key === 'g') pendingG = Date.now();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);
}

export function CrewGoKeys({ project }: { project: string | null }) {
  useCrewGoKeys(project);
  return null;
}
