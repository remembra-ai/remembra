import type { Ref } from 'react';
import clsx from 'clsx';
import { ChevronUp, X } from 'lucide-react';

/**
 * The 32px strip the desk folds down to: the signal square, `marshal`, the
 * rules-only status line, the toggle and ×. (No spans inside: .rr-win-bar
 * pushes a span to the right edge.)
 */
export function DeskBar({
  status,
  open,
  bodyId,
  onToggle,
  onClose,
  toggleRef,
}: {
  status: string;
  open: boolean;
  bodyId: string;
  onToggle: () => void;
  onClose: () => void;
  toggleRef?: Ref<HTMLButtonElement>;
}) {
  return (
    <div className="rr-win-bar h-8 shrink-0 py-0 pr-1.5">
      <i aria-hidden="true" />
      <button
        ref={toggleRef}
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={bodyId}
        className="flex h-full min-w-0 flex-1 items-center gap-2 text-left"
      >
        <b className="shrink-0 font-bold">marshal</b>
        <small className="min-w-0 flex-1 truncate text-[11px] font-medium opacity-70">{status}</small>
        <ChevronUp aria-hidden="true" className={clsx('h-3.5 w-3.5 shrink-0 opacity-70', open && 'rotate-180')} />
      </button>
      <button
        type="button"
        onClick={onClose}
        aria-label="Close Marshal"
        className="relative flex h-6 w-6 shrink-0 items-center justify-center opacity-70 after:absolute after:-inset-2.5 after:content-[''] hover:opacity-100 sm:after:hidden"
      >
        <X aria-hidden="true" className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
