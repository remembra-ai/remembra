// One channel message (§9.8): author with its key-verified / self-declared
// badge, the body as plain text (mentions set in mono), low-trust agent text
// collapsed behind "show anyway", and the human actions (edit own ≤10 min,
// pin, redact).

import { useId, useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { crewApi } from '../../../lib/crew/api';
import type { CrewState } from '../../../lib/crew/types';
import { absoluteTime, relativeTime } from '../../../lib/time';
import { AgentAvatar, Pill } from '../../relay/ui';
import { KIND_LABEL, authorOf, canEdit, decisionRef, type ChannelMessage } from './model';
import { PixelGlyph } from './pixels';
import { actionError } from './useChannel';

const MENTION_SPLIT = /((?<![A-Za-z0-9_@/.\\-])@[A-Za-z0-9][A-Za-z0-9._:-]{0,79})/g;

/** The body as React text nodes: mentions in mono, everything else verbatim (never HTML). */
function Body({ text }: { text: string }) {
  const parts = text.split(MENTION_SPLIT);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <span key={i} className="rounded-[2px] bg-paper-2 px-0.5 font-mono text-[0.92em] text-signal-ink">
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </>
  );
}

function Provenance({ provenance }: { provenance: string }) {
  if (provenance === 'key-verified') return <Pill tone="ok">key-verified</Pill>;
  if (provenance === 'self-declared') return <Pill title="The agent named itself; its key is not scoped to this agent">self-declared</Pill>;
  if (provenance === 'you') return <Pill>you</Pill>;
  return <Pill>{provenance}</Pill>;
}

function Avatar({ message, agentId }: { message: ChannelMessage; agentId: string | null }) {
  if (message.author_kind === 'agent') return <AgentAvatar agentId={agentId} size="sm" />;
  if (message.author_kind === 'system')
    return (
      <span aria-hidden="true" className="inline-flex h-6 w-6 items-center justify-center rounded-[3px] border border-rule text-ink-3">
        <PixelGlyph name="chat" size={12} />
      </span>
    );
  return (
    <span aria-hidden="true" className="inline-flex h-6 w-6 items-center justify-center rounded-[3px] bg-ink text-paper">
      <PixelGlyph name="crew" size={12} mono />
    </span>
  );
}

export function MessageItem({
  message,
  state,
  userId,
  human,
  now,
  replies,
  onOpenThread,
  onChanged,
  arrived,
  isRoot,
}: {
  message: ChannelMessage;
  state: CrewState | null;
  userId: string | null;
  human: boolean | null;
  now: Date;
  /** Reply count shown on a root in the stream (undefined inside a thread). */
  replies?: number;
  onOpenThread?: (rootId: string) => void;
  onChanged: (message: ChannelMessage) => void;
  arrived?: boolean;
  isRoot?: boolean;
}) {
  const author = authorOf(message, state, userId);
  const [showAnyway, setShowAnyway] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.body);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmRedact, setConfirmRedact] = useState(false);
  const editId = useId();
  const decision = message.refs.map((id) => state?.decisions[id]).find(Boolean) ?? null;
  const editable = canEdit(message, userId, now);

  const run = (what: string, action: () => Promise<{ message?: unknown }>, done: string) => {
    setBusy(what);
    action()
      .then((res) => {
        if (res && typeof res === 'object' && res.message && typeof res.message === 'object') onChanged(res.message as ChannelMessage);
        toast.success(done);
        setEditing(false);
        setConfirmRedact(false);
      })
      .catch((err: unknown) => toast.error(actionError(err)))
      .finally(() => setBusy(null));
  };

  let body: ReactNode;
  if (message.redacted) body = <p className="text-sm italic text-ink-3">Redacted by a human. The original is kept only as a hash.</p>;
  else if (message.collapsed && !showAnyway)
    body = (
      <p className="flex flex-wrap items-center gap-2 border-l-2 border-rule pl-2 text-sm text-ink-3">
        Collapsed: this reads like instructions aimed at agents (a heuristic, not a guarantee).
        <button type="button" onClick={() => setShowAnyway(true)} className="font-mono text-[11px] text-ink-2 underline underline-offset-2 hover:text-ink">
          show anyway
        </button>
      </p>
    );
  else if (editing)
    body = (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const text = draft.trim();
          if (!text || text === message.body) return setEditing(false);
          run('edit', () => crewApi.request<{ message?: unknown }>(`/messages/${encodeURIComponent(message.id)}`, { method: 'PATCH', body: { body: text } }).then((r) => r.data ?? {}), 'Edited. Agents that already saw it get "edited after delivery".');
        }}
        className="space-y-2"
      >
        <label htmlFor={editId} className="sr-only">
          Edit message
        </label>
        <textarea id={editId} value={draft} onChange={(e) => setDraft(e.target.value)} rows={3} className="rr-input w-full resize-y px-2.5 py-2 text-sm" />
        <div className="flex gap-2">
          <button type="submit" disabled={busy === 'edit'} className="rr-btn-primary px-2.5 py-1 text-xs">
            {busy === 'edit' ? 'Saving…' : 'Save edit'}
          </button>
          <button type="button" onClick={() => setEditing(false)} className="rr-btn-ghost px-2.5 py-1 text-xs">
            Cancel
          </button>
        </div>
      </form>
    );
  else
    body = (
      <p className="whitespace-pre-wrap text-[14px] leading-relaxed text-ink [overflow-wrap:anywhere]">
        <Body text={message.body} />
      </p>
    );

  return (
    <article
      aria-label={`${author.name}, ${KIND_LABEL[message.kind]}`}
      className={clsx('group relative flex gap-3 py-3 pl-1 pr-2', arrived && 'crew-flash', message.pinned && 'bg-signal-wash/40')}
    >
      <div className="relative z-10 pt-0.5">
        <Avatar message={message} agentId={author.agentId} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="font-mono text-[13px] font-bold text-ink">{author.name}</span>
          {author.kind === 'agent' && author.agentId && <span className="font-mono text-[11px] text-ink-3">{author.agentId}</span>}
          {author.provenance !== 'you' && <Provenance provenance={author.provenance} />}
          {message.kind !== 'chat' && (
            <Pill tone={message.kind === 'question' ? 'open' : message.kind === 'decision' || message.kind === 'answer' ? 'ok' : 'neutral'}>{KIND_LABEL[message.kind]}</Pill>
          )}
          {message.pinned && (
            <span className="inline-flex items-center gap-1 font-mono text-[11px] text-signal-ink">
              <PixelGlyph name="pin" size={10} /> pinned
            </span>
          )}
          {message.created_at && (
            <time dateTime={message.created_at} title={absoluteTime(message.created_at)} className="font-mono text-[11px] text-ink-3">
              {relativeTime(message.created_at, now)}
            </time>
          )}
          {message.edited && !message.redacted && <span className="font-mono text-[11px] text-ink-3">edited</span>}
        </p>
        <div className="mt-1">{body}</div>
        {decision && !message.redacted && (
          <p className="mt-1.5 inline-flex items-center gap-1.5 font-mono text-[11px] text-ink-2">
            <PixelGlyph name="decision" size={10} />
            {decisionRef(decision)} ·{' '}
            {decision.state === 'in_force' ? 'in force' : decision.state === 'proposed' ? 'waiting for a human to confirm' : decision.state}
          </p>
        )}
        {(replies !== undefined || editable || human) && !editing && (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-ink-3">
            {isRoot && onOpenThread && (
              <button type="button" onClick={() => onOpenThread(message.id)} className="hover:text-ink hover:underline">
                {replies ? `${replies} repl${replies === 1 ? 'y' : 'ies'} →` : 'Reply'}
              </button>
            )}
            {editable && !message.redacted && (
              <button type="button" onClick={() => { setDraft(message.body); setEditing(true); }} className="opacity-100 hover:text-ink sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100">
                Edit
              </button>
            )}
            {human && !message.redacted && (
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => run('pin', () => crewApi.pinMessage(message.id, !message.pinned), message.pinned ? 'Unpinned' : 'Pinned')}
                className="opacity-100 hover:text-ink sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100"
              >
                {busy === 'pin' ? <Loader2 className="inline h-3 w-3 animate-spin" aria-hidden="true" /> : message.pinned ? 'Unpin' : 'Pin'}
              </button>
            )}
            {human && !message.redacted && !confirmRedact && (
              <button type="button" onClick={() => setConfirmRedact(true)} className="opacity-100 hover:text-fail sm:opacity-0 sm:group-hover:opacity-100 sm:focus:opacity-100">
                Redact
              </button>
            )}
          </div>
        )}
        {confirmRedact && (
          <div role="group" aria-label="Confirm redaction" className="mt-2 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            <p>Redact this message? Its text is replaced for everyone; only a hash is kept.</p>
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => run('redact', () => crewApi.redactMessage(message.id), 'Redacted')}
                className="rr-btn-primary px-2.5 py-1 text-xs"
              >
                {busy === 'redact' ? 'Redacting…' : 'Redact'}
              </button>
              <button type="button" onClick={() => setConfirmRedact(false)} className="rr-btn-ghost px-2.5 py-1 text-xs">
                Keep it
              </button>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}
