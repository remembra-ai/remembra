// Navigation model: every page is a tab; tabs are grouped into the sections
// shown in the sidebar / bottom bar. The current tab and its parameters live
// in the URL hash (#/trail?agent=codex) so the back button, reloads and
// shared links work, including on a phone.

import { useMemo, useSyncExternalStore } from 'react';

export type TabType =
  | 'home'
  | 'trail'
  | 'agents'
  | 'inbox'
  | 'memories'
  | 'timeline'
  | 'analytics'
  | 'decay'
  | 'debugger'
  | 'graph'
  | 'entities'
  | 'brain'
  | 'settings'
  | 'keys'
  | 'billing'
  | 'teams'
  | 'projects'
  | 'admin';

export type SectionId = 'home' | 'trail' | 'agents' | 'inbox' | 'memory' | 'graph' | 'settings' | 'admin';

export interface TabMeta {
  label: string;
  title: string;
  subtitle: string;
}

export const TABS: Record<TabType, TabMeta> = {
  home: { label: 'Home', title: 'Mission control', subtitle: 'What your agents did, what they left open, and what is next.' },
  trail: { label: 'Trail', title: 'Trail', subtitle: 'git log for your agents: every handoff, newest first.' },
  agents: { label: 'Agents', title: 'Agents', subtitle: 'Every agent identity that has left a trail.' },
  inbox: { label: 'Inbox', title: 'Inbox', subtitle: 'Notes between agents. Write one and it leads their next brief.' },
  memories: { label: 'Memories', title: 'Memory', subtitle: 'Everything your agents and you have stored.' },
  timeline: { label: 'Timeline', title: 'Memory', subtitle: 'Memory creation and change over time.' },
  analytics: { label: 'Analytics', title: 'Memory', subtitle: 'Recall traffic, storage growth and usage.' },
  decay: { label: 'Decay', title: 'Memory', subtitle: 'Recency, retention, and what is being forgotten.' },
  debugger: { label: 'Debugger', title: 'Memory', subtitle: 'Why a recall returned what it did.' },
  graph: { label: 'Graph', title: 'Graph', subtitle: 'How memories connect through people, projects and ideas.' },
  entities: { label: 'Entities', title: 'Graph', subtitle: 'The people, products and concepts memory has resolved.' },
  brain: { label: 'Brain', title: 'Graph', subtitle: 'Themes, central memories and surprising links.' },
  settings: { label: 'General', title: 'Settings', subtitle: 'Profile, preferences and workspace defaults.' },
  keys: { label: 'API keys', title: 'Settings', subtitle: 'Keys for your agents, apps and automation.' },
  billing: { label: 'Billing', title: 'Settings', subtitle: 'Plan, usage and invoices.' },
  teams: { label: 'Teams', title: 'Settings', subtitle: 'Shared memory across people and agents.' },
  projects: { label: 'Projects', title: 'Settings', subtitle: 'Memory workspaces and their boundaries.' },
  admin: { label: 'Admin', title: 'Admin', subtitle: 'Operate the service.' },
};

export const SECTIONS: { id: SectionId; label: string; tabs: TabType[]; adminOnly?: boolean }[] = [
  { id: 'home', label: 'Home', tabs: ['home'] },
  { id: 'trail', label: 'Trail', tabs: ['trail'] },
  { id: 'agents', label: 'Agents', tabs: ['agents'] },
  { id: 'inbox', label: 'Inbox', tabs: ['inbox'] },
  { id: 'memory', label: 'Memory', tabs: ['memories', 'timeline', 'analytics', 'decay', 'debugger'] },
  { id: 'graph', label: 'Graph', tabs: ['graph', 'entities', 'brain'] },
  { id: 'settings', label: 'Settings', tabs: ['settings', 'keys', 'billing', 'teams', 'projects'] },
  { id: 'admin', label: 'Admin', tabs: ['admin'], adminOnly: true },
];

export function sectionOf(tab: TabType) {
  return SECTIONS.find((section) => section.tabs.includes(tab)) ?? SECTIONS[0];
}

export function isTab(value: string | null | undefined): value is TabType {
  return !!value && Object.prototype.hasOwnProperty.call(TABS, value);
}

export interface Route {
  tab: TabType;
  params: URLSearchParams;
}

export function parseHash(hash: string): Route {
  const text = hash.replace(/^#\/?/, '');
  const [path, search = ''] = text.split('?');
  const tab = isTab(path) ? path : 'home';
  return { tab, params: new URLSearchParams(search) };
}

export function hrefFor(tab: TabType, params?: Record<string, string | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value) search.set(key, value);
  }
  const text = search.toString();
  return `#/${tab}${text ? `?${text}` : ''}`;
}

/** Go to a tab (adds a history entry; `replace` swaps the current one). */
export function navigate(tab: TabType, params?: Record<string, string | null | undefined>, replace = false): void {
  const href = hrefFor(tab, params);
  if (window.location.hash === href) return;
  if (replace) {
    window.history.replaceState(window.history.state, '', href);
    window.dispatchEvent(new HashChangeEvent('hashchange'));
  } else {
    window.location.hash = href;
  }
}

function subscribe(callback: () => void): () => void {
  window.addEventListener('hashchange', callback);
  return () => window.removeEventListener('hashchange', callback);
}

function snapshot(): string {
  return window.location.hash;
}

/** The current route, re-rendering on navigation. */
export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, snapshot, () => '');
  return useMemo(() => parseHash(hash), [hash]);
}
