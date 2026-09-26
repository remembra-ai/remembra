// The lane menu (spec §9.3 item 7, shortcut `.` on a focused lane): Request
// checkpoint · Pause/Resume · Message · Hand over zones… · Release all claims
// (H) · Open agent page · Copy pickup command. Human-only items are disabled,
// with the reason, when the page is signed in with an API key.

import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import clsx from 'clsx';
import { MoreHorizontal } from 'lucide-react';
import type { LaneActionId } from './actions';

export type LaneMenuItem =
  | { kind: 'action'; id: LaneActionId; label: string; human: boolean; disabled?: string }
  | { kind: 'link'; id: string; label: string; href: string }
  | { kind: 'copy'; id: string; label: string; text: string };

export function LaneMenu({
  label,
  items,
  canAct,
  onAction,
  onCopy,
  open,
  onOpenChange,
}: {
  label: string;
  items: LaneMenuItem[];
  /** The signed-in principal is a human (dashboard login). */
  canAct: boolean;
  onAction: (id: LaneActionId) => void;
  onCopy: (text: string) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const menuId = useId();
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [active, setActive] = useState(0);

  useEffect(() => {
    if (!open) return;
    const nodes = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
    nodes?.[0]?.focus();
    const onDoc = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node) && e.target !== buttonRef.current) onOpenChange(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open, onOpenChange]);

  const close = () => {
    onOpenChange(false);
    buttonRef.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const nodes = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const next = (active + (e.key === 'ArrowDown' ? 1 : nodes.length - 1)) % Math.max(1, nodes.length);
      setActive(next);
      nodes[next]?.focus();
    } else if (e.key === 'Tab') {
      onOpenChange(false);
    }
  };

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`${label} menu`}
        title="Lane menu (.)"
        onClick={() => onOpenChange(!open)}
        className="rr-btn-ghost inline-flex h-8 w-8 items-center justify-center"
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>
      {open && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={`${label} actions`}
          onKeyDown={onKeyDown}
          className="modal-surface absolute right-0 top-9 z-30 w-60 rounded-[3px] py-1"
        >
          {items.map((item) => {
            const humanBlocked = item.kind === 'action' && item.human && !canAct;
            const disabled = item.kind === 'action' ? (item.disabled ?? (humanBlocked ? 'Needs a dashboard login' : undefined)) : undefined;
            const cls = clsx(
              'flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm',
              disabled ? 'cursor-not-allowed text-ink-3' : 'text-ink hover:bg-signal-wash focus:bg-signal-wash focus:outline-none',
            );
            if (item.kind === 'link') {
              return (
                <a key={item.id} role="menuitem" tabIndex={-1} href={item.href} className={cls} onClick={() => onOpenChange(false)}>
                  {item.label}
                </a>
              );
            }
            return (
              <button
                key={item.id}
                type="button"
                role="menuitem"
                tabIndex={-1}
                aria-disabled={!!disabled}
                title={disabled}
                className={cls}
                onClick={() => {
                  if (disabled) return;
                  onOpenChange(false);
                  if (item.kind === 'copy') onCopy(item.text);
                  else onAction(item.id);
                }}
              >
                <span>{item.label}</span>
                {item.kind === 'action' && item.human && <span className="font-mono text-[10px] text-ink-3">H</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
