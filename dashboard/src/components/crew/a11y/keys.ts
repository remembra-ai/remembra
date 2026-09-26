// Crew keyboard shortcuts (§9.16): `g` then c / z / b / h / f jumps between
// the views of the crew on screen: channel, zones, board, home (the track)
// and feed. Outside a crew, `g c` opens the Crews site board. The global
// `g h` (Home) still applies outside a crew.

import type { CrewScreen } from '../../../lib/crew/routes';

export const CREW_GO_KEYS: Record<string, CrewScreen> = {
  c: 'channel',
  z: 'zones',
  b: 'board',
  h: 'track',
  f: 'feed',
};

export type GoTarget = { kind: 'crew'; screen: CrewScreen } | { kind: 'crews' } | null;

/** Where `g <key>` goes: a view of the current crew, the Crews board, or nowhere (not a crew key). */
export function crewGoTarget(key: string, project: string | null): GoTarget {
  const k = key.toLowerCase();
  if (project && CREW_GO_KEYS[k]) return { kind: 'crew', screen: CREW_GO_KEYS[k] };
  if (!project && k === 'c') return { kind: 'crews' };
  return null;
}

/** Keys that must never be taken while the viewer is typing or a dialog is open. */
export function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== 'string') return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!el.isContentEditable;
}

/** The key help lines shown on crew screens (the global list lives in the shortcuts dialog). */
export const CREW_KEY_HELP: { keys: string; what: string }[] = [
  { keys: 'g c · g z · g b · g h · g f', what: 'channel · zones · board · track · feed' },
  { keys: 'j / k', what: 'next / previous item' },
  { keys: 'Enter', what: 'open the item' },
  { keys: '?', what: 'all shortcuts' },
];
