// Inbox: agent-to-agent messages across every agent, and a composer. A
// message to an agent leads that agent's next session brief.

import { useId, useState, type FormEvent } from 'react';
import clsx from 'clsx';
import { toast } from 'sonner';
import { Check, CheckCheck, Loader2, PenLine, Send, Undo2, X } from 'lucide-react';
import { useRelayData } from '../hooks/relayData';
import { useNow, useResource } from '../hooks/useResource';
import {
  DASHBOARD_SENDER,
  WITHDRAWN_NOTE,
  isWithdrawn,
  relay,
  type InboxMessage,
  type InboxStatusFilter,
} from '../lib/relay';
import { CONNECTABLE_AGENTS, agentMeta } from '../lib/agents';
import { navigate, useRoute } from '../lib/nav';
import { absoluteTime, relativeTime } from '../lib/time';
import { AgentAvatar, ErrorNotice, Pill, StaleNotice, TrailSkeleton } from '../components/relay/ui';

const STATUS_TABS: { id: InboxStatusFilter; label: string }[] = [
  { id: 'open', label: 'Open' },
  { id: 'unread', label: 'Unread' },
  { id: 'all', label: 'All' },
];

type AckAction = 'read' | 'done' | 'withdraw';

const AGENT_ID = /^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}$/;

function subjectFrom(text: string): string {
  const first = text.trim().split('\n')[0].trim();
  return first.length > 120 ? `${first.slice(0, 117)}…` : first;
}

function StatusPill({ message }: { message: InboxMessage }) {
  const { status } = message;
  if (isWithdrawn(message)) return <Pill>withdrawn</Pill>;
  if (status === 'unread') return <Pill tone="open">{message.to_agent === DASHBOARD_SENDER ? 'unread' : 'waiting'}</Pill>;
  if (status === 'done') return <Pill tone="ok">done</Pill>;
  if (status === 'blocked' || status === 'rejected') return <Pill tone="fail">{status}</Pill>;
  return <Pill>{status}</Pill>;
}

function MessageRow({
  message,
  highlighted,
  pending,
  onAck,
  now,
}: {
  message: InboxMessage;
  highlighted: boolean;
  pending: boolean;
  onAck: (message: InboxMessage, action: AckAction) => void;
  now: Date;
}) {
  const [open, setOpen] = useState(highlighted);
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);
  const confirmId = useId();
  const bodyId = useId();
  const long = message.body.length > 90 || message.body.includes('\n');
  const from = agentMeta(message.from_agent);
  const to = agentMeta(message.to_agent);
  const openStatus = message.status === 'unread' || message.status === 'read';
  // Only messages to the user are the user's to read. A note to an agent stays
  // in that agent's session brief until the agent itself acks it.
  const forYou = message.to_agent === DASHBOARD_SENDER;
  const waitingForAgent = !forYou && message.status === 'unread';
  const withdrawn = isWithdrawn(message);
  return (
    <li className={clsx('px-4 py-3 sm:px-5', highlighted && 'bg-signal-wash')}>
      <div className="flex items-start gap-3">
        <span className="flex shrink-0 items-center gap-1 pt-0.5" aria-hidden="true">
          <AgentAvatar agentId={message.from_agent} size="sm" />
          <span className="font-mono text-xs text-ink-3">→</span>
          <AgentAvatar agentId={message.to_agent} size="sm" />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className={clsx('text-[15px] leading-snug text-ink [overflow-wrap:anywhere]', message.status === 'unread' ? 'font-bold' : 'font-semibold')}>
              {message.subject}
            </span>
            <StatusPill message={message} />
          </p>
          <p className="mt-0.5 font-mono text-[11px] text-ink-3">
            {from.name} → {to.name} ·{' '}
            <time dateTime={message.created_at} title={absoluteTime(message.created_at)}>
              {relativeTime(message.created_at, now)}
            </time>
          </p>
          {message.body.trim() !== message.subject.trim() && (
            <p
              id={bodyId}
              className={clsx('mt-1.5 whitespace-pre-wrap text-sm text-ink-2 [overflow-wrap:anywhere]', !open && 'line-clamp-2')}
            >
              {message.body}
            </p>
          )}
          {long && message.body.trim() !== message.subject.trim() && (
            <button
              type="button"
              onClick={() => setOpen(!open)}
              aria-expanded={open}
              aria-controls={bodyId}
              className="mt-1 font-mono text-[11px] text-ink-2 underline decoration-rule underline-offset-2 hover:text-ink"
            >
              {open ? 'show less' : 'show all'}
            </button>
          )}
          {message.ack_note && !withdrawn && (
            <p className="mt-1.5 border-l-2 border-rule pl-2 text-sm text-ink-2">
              <span className="font-mono text-[11px] text-ink-3">note from {to.name}:</span> {message.ack_note}
            </p>
          )}
          {withdrawn && <p className="mt-1.5 font-mono text-[11px] text-ink-3">You withdrew this before {to.name} picked it up.</p>}
          {waitingForAgent && !confirmWithdraw && (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
              <p className="font-mono text-[11px] text-ink-3">In {to.name}’s session brief until {to.name} acknowledges it.</p>
              <button
                type="button"
                disabled={pending}
                onClick={() => setConfirmWithdraw(true)}
                className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-xs disabled:opacity-50"
              >
                <Undo2 className="h-3.5 w-3.5" aria-hidden="true" /> Withdraw
              </button>
            </div>
          )}
          {waitingForAgent && confirmWithdraw && (
            <div role="group" aria-labelledby={confirmId} className="mt-2 border-l-[3px] border-fail bg-paper px-3 py-2">
              <p id={confirmId} className="text-sm text-ink">
                Withdraw this note? {to.name} will no longer see it in its session brief.
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => onAck(message, 'withdraw')}
                  className="rr-btn-primary inline-flex items-center gap-1.5 px-2.5 py-1 text-xs disabled:opacity-50"
                >
                  {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <Undo2 className="h-3.5 w-3.5" aria-hidden="true" />}
                  Withdraw delivery
                </button>
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => setConfirmWithdraw(false)}
                  className="rr-btn-ghost inline-flex items-center px-2.5 py-1 text-xs disabled:opacity-50"
                >
                  Keep it
                </button>
              </div>
            </div>
          )}
          {openStatus && !waitingForAgent && (
            <div className="mt-2 flex flex-wrap gap-2">
              {message.status === 'unread' && (
                <button
                  type="button"
                  disabled={pending}
                  onClick={() => onAck(message, 'read')}
                  className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-xs disabled:opacity-50"
                >
                  <Check className="h-3.5 w-3.5" aria-hidden="true" /> Mark read
                </button>
              )}
              <button
                type="button"
                disabled={pending}
                onClick={() => onAck(message, 'done')}
                title={forYou ? undefined : `${to.name} has read it; this closes it on ${to.name}’s behalf`}
                className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-xs disabled:opacity-50"
              >
                {pending ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <CheckCheck className="h-3.5 w-3.5" aria-hidden="true" />}
                Done
              </button>
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

function Composer({
  initialTo,
  suggestions,
  onSent,
  onClose,
}: {
  initialTo: string;
  suggestions: string[];
  onSent: () => void;
  onClose?: () => void;
}) {
  const [to, setTo] = useState(initialTo);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const toId = useId();
  const textId = useId();
  const hintId = useId();
  const listId = useId();
  const recipient = to.trim();
  const validTo = AGENT_ID.test(recipient);
  const subject = subjectFrom(text);
  const canSend = validTo && subject.length > 0 && !sending;

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (!canSend) return;
    setSending(true);
    setError(null);
    relay
      .sendMessage({ to_agent: recipient, subject, body: text.trim(), from_agent: DASHBOARD_SENDER })
      .then(() => {
        toast.success(`Sent to ${agentMeta(recipient).name}`);
        setSentTo(recipient);
        setText('');
        onSent();
      })
      .catch((err: unknown) => setError(err))
      .finally(() => setSending(false));
  };

  return (
    <form onSubmit={submit} className="rr-card rounded-[3px]" aria-labelledby={`${toId}-title`}>
      <div className="flex items-start justify-between gap-3 px-4 pt-4 sm:px-5">
        <div>
          <p className="rr-eyebrow">New message</p>
          <h2 id={`${toId}-title`} className="font-display mt-1 text-lg font-bold text-ink">
            Write to an agent
          </h2>
        </div>
        {onClose && (
          <button type="button" onClick={onClose} className="rounded-[2px] p-1.5 text-ink-3 hover:bg-paper-2 hover:text-ink" aria-label="Close the composer">
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
      <div className="space-y-3 px-4 pb-4 pt-3 sm:px-5">
        <div>
          <label htmlFor={toId} className="block text-sm font-semibold text-ink">
            To
          </label>
          <input
            id={toId}
            value={to}
            onChange={(e) => {
              setTo(e.target.value);
              setSentTo(null);
            }}
            list={listId}
            required
            autoComplete="off"
            spellCheck={false}
            placeholder="claude-code"
            aria-invalid={recipient.length > 0 && !validTo}
            aria-describedby={hintId}
            className="rr-input mt-1 w-full px-3 py-2 font-mono text-sm"
          />
          <datalist id={listId}>
            {suggestions.map((id) => (
              <option key={id} value={id}>
                {agentMeta(id).name}
              </option>
            ))}
          </datalist>
          {suggestions.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Pick an agent">
              {suggestions.slice(0, 8).map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => {
                    setTo(id);
                    setSentTo(null);
                  }}
                  aria-pressed={recipient === id}
                  className={clsx(
                    'inline-flex items-center gap-1.5 rounded-[2px] border px-2 py-1 font-mono text-[11px]',
                    recipient === id ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink',
                  )}
                >
                  <AgentAvatar agentId={id} size="sm" />
                  {id}
                </button>
              ))}
            </div>
          )}
          <p id={hintId} className={clsx('mt-1.5 text-xs', recipient && !validTo ? 'text-fail' : 'text-ink-3')}>
            {recipient && !validTo
              ? 'Agent ids use letters, digits and . _ : @ / + - (no spaces).'
              : 'Use the exact agent id (as on the Agents page) so it lands in that agent’s brief.'}
          </p>
        </div>
        <div>
          <label htmlFor={textId} className="block text-sm font-semibold text-ink">
            Message
          </label>
          <textarea
            id={textId}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submit();
            }}
            required
            rows={4}
            maxLength={50000}
            placeholder="When you're back, fix the rounding test."
            className="rr-input mt-1 w-full resize-y px-3 py-2 text-sm"
          />
          <p className="mt-1 text-xs text-ink-3">The first line becomes the subject. ⌘/Ctrl + Enter sends.</p>
        </div>
        {error != null && <ErrorNotice compact error={error} what="the message" />}
        {sentTo && (
          <p role="status" className="border-l-[3px] border-ok bg-ok-wash px-3 py-2 text-sm text-ink">
            Sent. {agentMeta(sentTo).name} sees it at the top of its next session brief.
          </p>
        )}
        <button type="submit" disabled={!canSend} className="rr-btn-primary inline-flex w-full items-center justify-center gap-2 px-4 py-2.5 text-sm">
          {sending ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <Send className="h-4 w-4" aria-hidden="true" />}
          {validTo ? `Send to ${agentMeta(recipient).name}` : 'Send'}
        </button>
      </div>
    </form>
  );
}

export function Inbox() {
  const { summary, inbox: counts, refreshAll } = useRelayData();
  const { params } = useRoute();
  const now = useNow(30000);
  const statusParam = params.get('status');
  const status: InboxStatusFilter = statusParam === 'unread' || statusParam === 'all' ? statusParam : 'open';
  const agent = params.get('agent') || null;
  const highlight = params.get('open');
  const composeParam = params.get('compose') === '1';
  const toParam = params.get('to') || '';
  const [composeOpen, setComposeOpen] = useState(composeParam);
  const [lastComposeParam, setLastComposeParam] = useState(composeParam);
  if (composeParam !== lastComposeParam) {
    setLastComposeParam(composeParam);
    if (composeParam) setComposeOpen(true);
  }
  const [pending, setPending] = useState<Set<string>>(() => new Set());

  const list = useResource(`inbox:${status}:${agent ?? '*'}`, () => relay.inboxMessages({ status, agentId: agent, limit: 100 }), {
    pollMs: 30000,
  });

  const setFilter = (next: { status?: InboxStatusFilter; agent?: string | null }) =>
    navigate('inbox', {
      status: (next.status ?? status) === 'open' ? null : (next.status ?? status),
      agent: next.agent !== undefined ? next.agent : agent,
    });

  const ack = (message: InboxMessage, action: AckAction) => {
    setPending((prev) => new Set(prev).add(message.inbox_id));
    const request =
      action === 'withdraw'
        ? relay.ack(message.inbox_id, 'done', WITHDRAWN_NOTE)
        : relay.ack(message.inbox_id, action === 'done' ? 'done' : undefined);
    request
      .then(() => {
        toast.success(
          action === 'withdraw'
            ? `Withdrawn. ${agentMeta(message.to_agent).name} won’t see it.`
            : action === 'done'
              ? 'Marked done'
              : 'Marked read',
        );
        list.refresh();
        refreshAll();
      })
      .catch((err: unknown) => {
        toast.error(err instanceof Error ? err.message : 'Could not update the message');
      })
      .finally(() =>
        setPending((prev) => {
          const next = new Set(prev);
          next.delete(message.inbox_id);
          return next;
        }),
      );
  };

  // Agents that have left a trail or used the inbox first, then the ones the relay can connect.
  const known = new Set<string>();
  for (const a of summary.data?.agents ?? []) known.add(a.agent_id);
  for (const a of counts.data?.agents ?? []) if (a.agent_id !== DASHBOARD_SENDER) known.add(a.agent_id);
  for (const id of CONNECTABLE_AGENTS) known.add(id);
  const suggestions = [...known];
  const agentFilterOptions = [...new Set([...(counts.data?.agents ?? []).map((a) => a.agent_id), ...(agent ? [agent] : [])])];

  const items = list.data?.items ?? [];

  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
      <div className="min-w-0 space-y-4">
        <div className="rr-card flex flex-wrap items-center gap-3 rounded-[3px] px-4 py-3 sm:px-5">
          <div role="tablist" aria-label="Messages to show" className="flex gap-1">
            {STATUS_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={status === tab.id}
                onClick={() => setFilter({ status: tab.id })}
                className={clsx(
                  'rounded-[2px] border px-2.5 py-1.5 font-mono text-xs',
                  status === tab.id ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink hover:text-ink',
                )}
              >
                {tab.label}
                {tab.id === 'unread' && counts.data && counts.data.unread_total > 0 && (
                  <span className="ml-1.5 tabular">{counts.data.unread_total}</span>
                )}
              </button>
            ))}
          </div>
          <label className="ml-auto flex items-center gap-2 text-sm text-ink-2">
            <span className="font-mono text-xs">agent</span>
            <select
              value={agent ?? ''}
              onChange={(e) => setFilter({ agent: e.target.value || null })}
              className="rr-input px-2 py-1.5 font-mono text-xs"
            >
              <option value="">all agents</option>
              {agentFilterOptions.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          {!composeOpen && (
            <button
              type="button"
              onClick={() => setComposeOpen(true)}
              className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-sm lg:hidden"
            >
              <PenLine className="h-4 w-4" aria-hidden="true" /> Write
            </button>
          )}
        </div>

        {composeOpen && (
          <div className="lg:hidden">
            <Composer
              key={`m-${toParam}`}
              initialTo={toParam}
              suggestions={suggestions}
              onSent={() => {
                list.refresh();
                refreshAll();
              }}
              onClose={() => setComposeOpen(false)}
            />
          </div>
        )}

        <div className="rr-card rounded-[3px]">
          {list.loading && <TrailSkeleton rows={4} />}
          {!list.data && !list.loading && list.error != null && <ErrorNotice error={list.error} what="the inbox" onRetry={list.refresh} />}
          {list.data && list.error != null && <StaleNotice error={list.error} what="the inbox" />}
          {list.data && items.length === 0 && (
            <div className="px-5 py-10 text-center">
              <p className="font-display text-xl font-bold text-ink">
                {status === 'all' ? 'No messages yet.' : status === 'unread' ? 'Nothing unread.' : 'No open messages.'}
              </p>
              <p className="mx-auto mt-1 max-w-md text-sm text-ink-2">
                Agents leave each other notes with the <code className="font-mono text-[13px]">send_to_inbox</code> tool. Write one here and
                it appears at the top of that agent’s next session brief.
              </p>
              {status !== 'all' && (
                <button type="button" onClick={() => setFilter({ status: 'all' })} className="rr-btn-ghost mt-4 px-3 py-2 text-sm">
                  Show all messages
                </button>
              )}
            </div>
          )}
          {items.length > 0 && (
            <ul className="divide-y divide-rule" aria-label="Messages">
              {items.map((message) => (
                <MessageRow
                  key={message.inbox_id}
                  message={message}
                  highlighted={message.inbox_id === highlight}
                  pending={pending.has(message.inbox_id)}
                  onAck={ack}
                  now={now}
                />
              ))}
            </ul>
          )}
          {list.data && list.data.total > items.length && (
            <p className="border-t border-rule px-4 py-3 text-center font-mono text-[11px] text-ink-3">
              Showing the newest {items.length} of {list.data.total}. Filter by agent to narrow it down.
            </p>
          )}
        </div>
      </div>

      <div className="hidden min-w-0 lg:block">
        <div className="sticky top-4">
          <Composer
            key={`d-${toParam}`}
            initialTo={toParam}
            suggestions={suggestions}
            onSent={() => {
              list.refresh();
              refreshAll();
            }}
          />
        </div>
      </div>
    </div>
  );
}
