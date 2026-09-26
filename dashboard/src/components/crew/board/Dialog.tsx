// A focus-trapped modal sheet: centred on desktop, a bottom sheet on a phone.
// Escape and the backdrop close it; focus returns to where it was.

import { useEffect, useRef, type ReactNode } from 'react';
import clsx from 'clsx';
import { X } from 'lucide-react';

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({
  title,
  eyebrow,
  onClose,
  children,
  wide,
  side,
  labelId,
}: {
  title: ReactNode;
  eyebrow?: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
  /** A drawer on the right (desktop) instead of a centred sheet. */
  side?: boolean;
  labelId: string;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const first = panel?.querySelector<HTMLElement>('[data-autofocus]') ?? panel?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab' || !panel) return;
      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null);
      if (!items.length) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
      previous?.focus?.();
    };
  }, []);

  return (
    <div
      className={clsx(
        'crew-board fixed inset-0 z-[100] flex items-end justify-center',
        side ? 'sm:items-stretch sm:justify-end' : 'sm:items-start sm:px-4 sm:pt-[8vh]',
      )}
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelId}
    >
      <button type="button" aria-label="Close" tabIndex={-1} className="modal-backdrop absolute inset-0 h-full w-full" onClick={onClose} />
      <div
        ref={panelRef}
        className={clsx(
          'modal-surface relative flex max-h-[88vh] w-full flex-col rounded-t-[4px]',
          side ? 'sm:max-h-none sm:max-w-xl sm:rounded-none sm:border-y-0 sm:border-r-0' : 'sm:rounded-[3px]',
          !side && (wide ? 'sm:max-w-2xl' : 'sm:max-w-lg'),
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b border-rule px-4 py-3 sm:px-5">
          <div className="min-w-0">
            {eyebrow && <p className="rr-eyebrow">{eyebrow}</p>}
            <h2 id={labelId} className="font-display mt-0.5 text-lg font-bold leading-tight text-ink">
              {title}
            </h2>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="shrink-0 rounded-[2px] p-1.5 text-ink-3 hover:text-ink">
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">{children}</div>
      </div>
    </div>
  );
}
