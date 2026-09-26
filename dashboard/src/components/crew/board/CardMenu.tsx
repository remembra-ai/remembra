// The card's action menu (the keyboard and phone path for everything a drag
// does): open, close with a report or waiver, review, pick up a stalled
// baton, reopen, open the receipt. Arrow keys move, Escape closes.

import { useEffect, useRef } from 'react';

export interface MenuItem {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

export function CardMenu({
  items,
  anchor,
  onClose,
  label,
}: {
  items: MenuItem[];
  anchor: { left: number; top: number; bottom: number; right: number };
  onClose: () => void;
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const menu = ref.current;
    const buttons = () => [...(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];
    buttons()[0]?.focus();
    const onKey = (e: KeyboardEvent) => {
      const list = buttons();
      const i = list.indexOf(document.activeElement as HTMLButtonElement);
      if (e.key === 'Escape') {
        e.preventDefault();
        onCloseRef.current();
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        list[(i + 1) % list.length]?.focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        list[(i - 1 + list.length) % list.length]?.focus();
      } else if (e.key === 'Tab') {
        onCloseRef.current();
      }
    };
    const onDown = (e: MouseEvent) => {
      if (menu && !menu.contains(e.target as Node)) onCloseRef.current();
    };
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('mousedown', onDown, true);
    window.addEventListener('scroll', onCloseRef.current, true);
    const close = onCloseRef.current;
    return () => {
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('mousedown', onDown, true);
      window.removeEventListener('scroll', close, true);
    };
  }, []);

  const width = 232;
  const left = Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8));
  const below = anchor.bottom + 4;
  const top = below + items.length * 40 > window.innerHeight ? Math.max(8, anchor.top - items.length * 40 - 4) : below;

  return (
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      className="modal-surface fixed z-[90] rounded-[3px] py-1"
      style={{ left, top, width }}
    >
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          role="menuitem"
          onClick={() => {
            onClose();
            item.run();
          }}
          className="cmdk-item w-full text-left text-[13px]"
        >
          <span className="min-w-0">
            <span className="block text-ink">{item.label}</span>
            {item.hint && <span className="block text-[11px] text-ink-3">{item.hint}</span>}
          </span>
        </button>
      ))}
    </div>
  );
}
