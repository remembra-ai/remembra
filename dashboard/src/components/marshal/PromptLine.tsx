import type { Ref } from 'react';
import clsx from 'clsx';
import { COUNTER_FROM, MAX_QUESTION_CHARS, countChars, isSubmitKey } from '../../lib/marshalDesk';

/**
 * `marshal ›` and the question. Enter asks it and does nothing else. While a
 * question is out, or while the notice above says the model is off, the line
 * is read-only (never `disabled`, so focus stays where it is). The block
 * cursor stands in for the caret while the line is empty.
 */
export function PromptLine({
  id,
  draft,
  asking,
  blocked,
  onDraft,
  onSubmit,
  inputRef,
}: {
  id: string;
  draft: string;
  asking: boolean;
  blocked: boolean;
  onDraft: (text: string) => void;
  onSubmit: () => void;
  inputRef?: Ref<HTMLInputElement>;
}) {
  const count = countChars(draft);
  const over = count > MAX_QUESTION_CHARS;
  const canAsk = !asking && !blocked && !over && draft.trim() !== '';
  const countId = `${id}-count`;
  return (
    <form
      className="flex shrink-0 items-center gap-2 border-t border-rule bg-panel px-4 py-1.5 focus-within:shadow-[inset_0_2px_0_var(--signal)]"
      onSubmit={(event) => {
        event.preventDefault();
        if (canAsk) onSubmit();
      }}
    >
      <label htmlFor={id} className="shrink-0 select-none font-mono text-[13px] text-ink-3">
        marshal ›
      </label>
      <div className="relative min-w-0 flex-1">
        <input
          id={id}
          ref={inputRef}
          type="text"
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            if (canAsk && isSubmitKey({ key: event.key, isComposing: event.nativeEvent.isComposing, keyCode: event.keyCode })) onSubmit();
          }}
          readOnly={asking || blocked}
          aria-label="Ask Marshal"
          aria-disabled={asking || blocked ? true : undefined}
          aria-invalid={over ? true : undefined}
          aria-describedby={count > COUNTER_FROM ? countId : undefined}
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="send"
          className={clsx(
            'min-h-11 w-full min-w-0 bg-transparent font-mono text-[13px] text-ink outline-none sm:min-h-9',
            blocked && 'cursor-not-allowed text-ink-3',
            (draft === '' || blocked) && 'caret-transparent',
          )}
        />
        {draft === '' && !blocked && (
          <i aria-hidden="true" className="rr-desk-cursor pointer-events-none absolute left-0 top-1/2 -translate-y-1/2" />
        )}
      </div>
      {count > COUNTER_FROM && (
        <output id={countId} className={clsx('tabular shrink-0 font-mono text-[11px]', over ? 'text-fail' : 'text-ink-3')}>
          {count}/{MAX_QUESTION_CHARS}
        </output>
      )}
      <button
        type="submit"
        disabled={!canAsk}
        className="rr-btn-ghost min-h-11 shrink-0 px-2.5 font-mono text-[11px] disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0 sm:py-1"
      >
        ask
      </button>
    </form>
  );
}
