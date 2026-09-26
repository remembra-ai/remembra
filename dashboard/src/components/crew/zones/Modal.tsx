// Focus-trapped overlay used by the zone drawer and the action dialogs (§9.16:
// focus-trapped drawers). Esc or the scrim closes; focus returns to where it was.

import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

const FOCUSABLE = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Modal({
  labelledBy,
  onClose,
  className,
  children,
  initialFocus,
  top = false,
}: {
  labelledBy: string;
  onClose: () => void;
  className: string;
  children: ReactNode;
  /** CSS selector of the element to focus first (default: the first focusable). */
  initialFocus?: string;
  /** Stack above the drawer (a dialog opened from inside it). */
  top?: boolean;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    const el = box.current;
    const first = (initialFocus && el?.querySelector<HTMLElement>(initialFocus)) || el?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? el)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        closeRef.current();
        return;
      }
      if (e.key !== 'Tab' || !el) return;
      const items = [...el.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (!items.length) return;
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstItem) {
        e.preventDefault();
        lastItem.focus();
      } else if (!e.shiftKey && document.activeElement === lastItem) {
        e.preventDefault();
        firstItem.focus();
      }
    };
    el?.addEventListener('keydown', onKey);
    return () => {
      el?.removeEventListener('keydown', onKey);
      before?.focus?.();
    };
  }, [initialFocus]);

  return createPortal(
    <div className="cz-root">
      <div className="cz-scrim" data-top={top ? 'true' : undefined} aria-hidden="true" onClick={() => closeRef.current()} />
      <div ref={box} role="dialog" aria-modal="true" aria-labelledby={labelledBy} tabIndex={-1} className={className}>
        {children}
      </div>
    </div>,
    document.body,
  );
}
