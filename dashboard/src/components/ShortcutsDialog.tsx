import { useEffect, useRef } from 'react';
import { X } from 'lucide-react';
import { GO_KEYS } from '../hooks/useShortcuts';
import { sectionOf } from '../lib/nav';

function Keys({ keys }: { keys: string[] }) {
  return (
    <span className="flex shrink-0 items-center gap-1">
      {keys.map((key, index) => (
        <kbd key={index} className="min-w-[22px] rounded-[2px] border border-rule bg-paper px-1.5 py-0.5 text-center font-mono text-[11px] text-ink">
          {key}
        </kbd>
      ))}
    </span>
  );
}

export function ShortcutsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      previous?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;
  const rows: { keys: string[]; label: string }[] = [
    { keys: ['⌘', 'K'], label: 'Search and commands' },
    { keys: ['/'], label: 'Search and commands' },
    { keys: ['c'], label: 'Write to an agent' },
    { keys: ['?'], label: 'This list' },
    ...Object.entries(GO_KEYS).map(([key, tab]) => ({
      keys: ['g', key],
      label: `Go to ${sectionOf(tab).label}`,
    })),
  ];
  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center px-4 pt-[12vh]" role="dialog" aria-modal="true" aria-labelledby="shortcuts-title">
      <button type="button" aria-label="Close" tabIndex={-1} className="modal-backdrop absolute inset-0 h-full w-full" onClick={onClose} />
      <div className="modal-surface relative w-full max-w-sm rounded-[3px]">
        <div className="flex items-center justify-between border-b border-rule px-4 py-3">
          <h2 id="shortcuts-title" className="font-display text-lg font-bold text-ink">
            Keyboard shortcuts
          </h2>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="Close" className="rounded-[2px] p-1.5 text-ink-3 hover:text-ink">
            <X className="h-4 w-4" />
          </button>
        </div>
        <ul className="divide-y divide-rule px-4 py-1">
          {rows.map((row, index) => (
            <li key={index} className="flex items-center justify-between gap-4 py-2 text-sm text-ink-2">
              {row.label}
              <Keys keys={row.keys} />
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
