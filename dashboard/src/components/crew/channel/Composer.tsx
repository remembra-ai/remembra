// The channel composer (§9.8): `@` autocomplete over what the server routes,
// `/decide` and `/freeze`, the kind (chat, note, question, answer), and the
// delivery expectation for whoever is mentioned. A retry after a network
// error reuses the draft's client_msg_id, so it can never post twice.

import { useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { crewApi } from '../../../lib/crew/api';
import type { CrewState, MessageKind, ZoneView } from '../../../lib/crew/types';
import {
  HUMAN_KINDS,
  SLASH_COMMANDS,
  activeCommand,
  activeMention,
  applyMention,
  deliveryLines,
  filterMentions,
  mentionOptions,
  newClientMsgId,
  parseComposer,
  type ChannelMessage,
  type HumanKind,
} from './model';
import { PixelGlyph } from './pixels';
import { actionError } from './useChannel';

interface Suggestion {
  insert: string;
  label: string;
  hint: string;
  command: boolean;
}

export function Composer({
  crewId,
  state,
  human,
  threadRootId,
  threadRootKind,
  onPosted,
}: {
  crewId: string;
  state: CrewState | null;
  human: boolean | null;
  /** Reply into this thread; null posts a new thread. */
  threadRootId: string | null;
  threadRootKind?: MessageKind | null;
  onPosted: (message: ChannelMessage | null) => void;
}) {
  const [text, setText] = useState('');
  const [caret, setCaret] = useState(0);
  const [kindChoice, setKindChoice] = useState<HumanKind | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [dismissedAt, setDismissedAt] = useState<string | null>(null);
  const clientMsgId = useRef(newClientMsgId());
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const inputId = useId();
  const hintId = useId();
  const listId = useId();

  const defaultKind: HumanKind = threadRootId && threadRootKind === 'question' ? 'answer' : 'chat';
  const kind = kindChoice ?? defaultKind;
  const kinds = threadRootId ? HUMAN_KINDS : HUMAN_KINDS.filter((k) => k !== 'answer');
  const zones: ZoneView[] = useMemo(() => (state ? Object.values(state.zones) : []), [state]);
  const options = useMemo(() => (state ? mentionOptions(state) : []), [state]);
  const intent = parseComposer(text, zones, kind);
  const lines = useMemo(() => (intent.type === 'message' ? deliveryLines(text, state) : []), [intent.type, text, state]);

  const suggestions: Suggestion[] = useMemo(() => {
    const cmd = activeCommand(text, caret);
    if (cmd !== null) {
      return SLASH_COMMANDS.filter((c) => c.name.startsWith(cmd)).map((c) => ({ insert: `/${c.name} `, label: c.usage, hint: c.hint, command: true }));
    }
    const m = activeMention(text, caret);
    if (!m) return [];
    return filterMentions(options, m.query).map((o) => ({ insert: o.token, label: `@${o.label}`, hint: o.hint, command: false }));
  }, [text, caret, options]);
  const open = suggestions.length > 0 && dismissedAt !== `${text}:${caret}`;

  const choose = (s: Suggestion) => {
    let next: { text: string; caret: number };
    if (s.command) {
      const rest = text.replace(/^\/\S*\s?/, '');
      next = { text: s.insert + rest, caret: s.insert.length };
    } else next = applyMention(text, caret, s.insert);
    setText(next.text);
    setCaret(next.caret);
    setActive(0);
    requestAnimationFrame(() => {
      const el = inputRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(next.caret, next.caret);
      }
    });
  };

  const reset = () => {
    setText('');
    setCaret(0);
    setKindChoice(null);
    clientMsgId.current = newClientMsgId();
  };

  const send = () => {
    if (sending || human === false) return;
    if (intent.type === 'empty') return;
    if (intent.type === 'invalid') {
      setError(intent.error);
      return;
    }
    setSending(true);
    setError(null);
    let call: Promise<ChannelMessage | null>;
    if (intent.type === 'freeze') {
      call = crewApi.freezeZone(intent.zone.id, intent.reason).then(() => {
        toast.success(`Zone ${intent.zone.slug} frozen. Agents are denied there until you unfreeze it.`);
        return null;
      });
    } else {
      const body = intent.body;
      const msgKind: MessageKind = intent.type === 'decide' ? 'decision' : intent.kind;
      call = crewApi
        .postMessage(crewId, { kind: msgKind, body, thread_root_id: threadRootId ?? undefined, clientMsgId: clientMsgId.current })
        .then((res) => {
          if (intent.type === 'decide') toast.success('Decision in force. It leads every agent brief from now on.');
          return (res.message as ChannelMessage | undefined) ?? null;
        });
    }
    call
      .then((message) => {
        reset();
        onPosted(message);
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setSending(false));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const step = e.key === 'ArrowDown' ? 1 : -1;
        setActive((i) => (i + step + suggestions.length) % suggestions.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        choose(suggestions[Math.min(active, suggestions.length - 1)]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setDismissedAt(`${text}:${caret}`);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      send();
    }
  };

  const syncCaret = () => setCaret(inputRef.current?.selectionStart ?? text.length);

  const sendLabel =
    intent.type === 'decide' ? 'Put in force' : intent.type === 'freeze' ? `Freeze ${intent.zone.slug}` : threadRootId ? 'Reply' : 'Post';

  if (human === false) {
    return (
      <p className="border-t border-rule px-4 py-3 text-sm text-ink-2 sm:px-5">
        You are signed in with an API key. Agents post through their crew session; sign in to the dashboard to write here as yourself.
      </p>
    );
  }

  return (
    <div className="border-t border-rule px-4 pb-4 pt-3 sm:px-5">
      <div className="mb-2 flex flex-wrap items-center gap-1.5" role="radiogroup" aria-label="Message kind">
        {kinds.map((k) => (
          <button
            key={k}
            type="button"
            role="radio"
            aria-checked={kind === k}
            onClick={() => setKindChoice(k)}
            disabled={intent.type === 'decide' || intent.type === 'freeze'}
            className={clsx(
              'rounded-[2px] border px-2 py-1 font-mono text-[11px] disabled:opacity-40',
              kind === k ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink hover:text-ink',
            )}
          >
            {k}
          </button>
        ))}
        <span className="ml-auto hidden font-mono text-[11px] text-ink-3 sm:inline">@ to mention · / for commands · Shift+Enter new line</span>
      </div>
      <div className="relative">
        <label htmlFor={inputId} className="sr-only">
          {threadRootId ? 'Reply in this thread' : 'Message the crew'}
        </label>
        <textarea
          ref={inputRef}
          id={inputId}
          value={text}
          rows={3}
          onChange={(e) => {
            setText(e.target.value);
            setCaret(e.target.selectionStart ?? e.target.value.length);
            setActive(0);
            setError(null);
          }}
          onKeyDown={onKeyDown}
          onKeyUp={syncCaret}
          onClick={syncCaret}
          placeholder={threadRootId ? 'Reply in thread…' : 'Message the crew. @cc-1, @crew, @zone:pos, @task:T-14, /decide, /freeze'}
          aria-describedby={hintId}
          aria-autocomplete="list"
          aria-controls={open ? listId : undefined}
          aria-expanded={open}
          aria-activedescendant={open ? `${listId}-${active}` : undefined}
          role="combobox"
          className="rr-input block w-full resize-y px-3 py-2.5 text-sm"
        />
        {open && (
          <ul
            id={listId}
            role="listbox"
            aria-label={suggestions[0]?.command ? 'Commands' : 'Mention'}
            className="rr-card absolute bottom-full left-0 z-20 mb-1 max-h-64 w-full max-w-md overflow-y-auto rounded-[3px] py-1"
          >
            {suggestions.map((s, i) => (
              <li
                key={s.insert}
                id={`${listId}-${i}`}
                role="option"
                aria-selected={i === active}
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(s);
                }}
                onMouseEnter={() => setActive(i)}
                className={clsx('flex cursor-pointer items-baseline gap-2 px-3 py-1.5', i === active && 'bg-signal-wash')}
              >
                <span className="shrink-0 font-mono text-[12px] font-bold text-ink">{s.label}</span>
                <span className="min-w-0 truncate text-xs text-ink-3">{s.hint}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div id={hintId} className="mt-2 space-y-0.5 font-mono text-[11px] leading-relaxed text-ink-3" aria-live="polite">
        {intent.type === 'decide' && (
          <p className="flex items-center gap-1.5 text-ink-2">
            <PixelGlyph name="decision" size={10} /> A decision you post is in force at once and appears in every agent brief as "confirmed by a human".
          </p>
        )}
        {intent.type === 'freeze' && (
          <p className="flex items-center gap-1.5 text-ink-2">
            <PixelGlyph name="zone" size={10} /> You take zone {intent.zone.slug} yourself: every agent is denied there from its next write until you unfreeze it.
          </p>
        )}
        {intent.type === 'invalid' && text.trim().startsWith('/') && !open && <p className="text-fail">{intent.error}</p>}
        {!open && lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-2 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}
      <div className="mt-2.5 flex justify-end">
        <button
          type="button"
          onClick={send}
          disabled={sending || intent.type === 'empty'}
          className="rr-btn-primary inline-flex items-center gap-2 px-4 py-2 text-sm disabled:opacity-50"
        >
          {sending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <PixelGlyph name="baton" size={14} mono />}
          {sendLabel}
        </button>
      </div>
    </div>
  );
}
