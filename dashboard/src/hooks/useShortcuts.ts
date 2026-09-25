import { useEffect, useRef } from 'react';
import { navigate, type TabType } from '../lib/nav';

/** `g` then a key jumps to a page. */
export const GO_KEYS: Record<string, TabType> = {
  h: 'home',
  t: 'trail',
  a: 'agents',
  i: 'inbox',
  m: 'memories',
  g: 'graph',
  s: 'settings',
};

function typingInto(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
}

/**
 * Global keyboard shortcuts: ⌘K / Ctrl+K or / for the command palette, ? for
 * the shortcut list, c to write a message, and g-then-key navigation. Keys
 * are ignored while typing in a field or when a modifier is held.
 */
export function useShortcuts(handlers: { onPalette: () => void; onHelp: () => void; enabled: boolean }): void {
  const ref = useRef(handlers);
  useEffect(() => {
    ref.current = handlers;
  });

  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      const { onPalette, onHelp, enabled } = ref.current;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        onPalette();
        return;
      }
      if (!enabled || e.metaKey || e.ctrlKey || e.altKey || typingInto(e.target)) return;
      if (document.querySelector('[aria-modal="true"]')) return;
      const key = e.key.toLowerCase();
      if (pendingG && Date.now() - pendingG < 1200) {
        pendingG = 0;
        const tab = GO_KEYS[key];
        if (tab) {
          e.preventDefault();
          navigate(tab);
        }
        return;
      }
      if (key === 'g') {
        pendingG = Date.now();
      } else if (e.key === '?') {
        e.preventDefault();
        onHelp();
      } else if (key === '/') {
        e.preventDefault();
        onPalette();
      } else if (key === 'c') {
        e.preventDefault();
        navigate('inbox', { compose: '1' });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
}
