// One inbox item (§9.9): what it is, where it came from, and its one primary
// action. Safety items carry the orange edge; agent-originated items say
// "from cc-2 (self-declared)" and show how many were coalesced. Needs-you
// items can also be marked done or dismissed (a human decision, D27).

import { useId, useState } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { crewApi } from '../../../lib/crew/api';
import type { CrewState, DecisionView, SessionView } from '../../../lib/crew/types';
import { absoluteTime, relativeTime } from '../../../lib/time';
import { useResource } from '../../../hooks/useResource';
import { Pill } from '../../relay/ui';
import { DecisionConfirm } from '../channel/DecisionConfirm';
import { PixelGlyph } from '../channel/pixels';
import type { GlyphName } from '../channel/dither';
import { newClientMsgId } from '../channel/model';
import { actionError, currentUserId } from '../channel/useChannel';
import { coalescedLabel, isSafety, kindLabel, originLabel, primaryAction, refHref, type InboxItem } from './model';

const GLYPH: Record<string, GlyphName> = {
  review_report: 'check',
  human_question: 'question',
  decision_to_confirm: 'decision',
  collision_escalated: 'collision',
  collision_open: 'collision',
  baton_available: 'baton',
  baton_waiting: 'baton',
  baton_reserved: 'baton',
  zone_change_pending: 'zone',
  zone_hoarding: 'zone',
  zone_contested: 'zone',
  stuck_agent: 'clock',
  idle_park: 'clock',
  githook_missing: 'hook',
  tamper_blocked: 'shield',
  bypass_used: 'shield',
  false_deny_alarm: 'shield',
  task_ready: 'check',
  task_blocked: 'collision',
  mention: 'chat',
};

function takenBy(claimedBy: string | null | undefined, sessions: readonly SessionView[]): string {
  if (!claimedBy) return 'taken';
  if (claimedBy === currentUserId()) return 'taken by you';
  if (claimedBy.startsWith('cs_')) return `taken by ${sessions.find((s) => s.id === claimedBy)?.callsign ?? 'an agent'}`;
  return 'taken by a teammate';
}

/** Proposed decisions of the item's crew, each with Confirm / Reject (live state first, else REST). */
function ProposedDecisions({
  crewId,
  state,
  sessions,
  onDone,
}: {
  crewId: string;
  state: CrewState | null;
  sessions: readonly SessionView[];
  onDone: () => void;
}) {
  const res = useResource(state ? null : `decisions:${crewId}`, () => crewApi.decisions(crewId));
  const list: DecisionView[] = state
    ? Object.values(state.decisions).filter((d) => d.state === 'proposed')
    : ((res.data?.items ?? []) as DecisionView[]).filter((d) => d.state === 'proposed');
  if (!state && res.loading) return <p className="font-mono text-[11px] text-ink-3">Loading the proposed decisions…</p>;
  if (!list.length) return <p className="text-sm text-ink-3">No decision is waiting any more.</p>;
  return (
    <div className="space-y-3">
      {list.map((d) => (
        <div key={d.id} className="border-l-2 border-rule pl-3">
          <DecisionConfirm
            decision={d}
            state={state}
            human={null}
            compact
            sessions={sessions}
            onDone={() => {
              res.refresh();
              onDone();
            }}
          />
        </div>
      ))}
    </div>
  );
}

function Answer({ item, onDone }: { item: InboxItem; onDone: () => void }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  // One id per draft (the server accepts [A-Za-z0-9._:-]{1,64}): a retry never posts twice.
  const [clientMsgId] = useState(newClientMsgId);
  const send = () => {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    crewApi
      .postMessage(item.crew_id, { kind: 'answer', body, reply_to_id: item.ref_id ?? undefined, clientMsgId })
      .then(async () => {
        if (item.coalesced_count <= 1) await crewApi.inboxItem(item.id, 'resolve').catch(() => undefined);
        toast.success(item.coalesced_count > 1 ? 'Answered. The other questions are still in the channel.' : 'Answered. The agent sees it at its next turn.');
        setText('');
        onDone();
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setBusy(false));
  };
  return (
    <div className="space-y-2">
      <label htmlFor={id} className="sr-only">
        Your answer
      </label>
      <textarea
        id={id}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) send();
        }}
        rows={2}
        placeholder="Your answer (⌘/Ctrl + Enter sends)"
        className="rr-input w-full resize-y px-2.5 py-2 text-sm"
      />
      {error && (
        <p role="alert" className="text-xs text-fail">
          {error}
        </p>
      )}
      <button type="button" onClick={send} disabled={busy || !text.trim()} className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs">
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />} Send answer
      </button>
    </div>
  );
}

export function NeedsYouCard({
  item,
  state,
  sessions,
  now,
  showProject,
  onChanged,
}: {
  item: InboxItem;
  state: CrewState | null;
  sessions: readonly SessionView[];
  now: Date;
  showProject: boolean;
  onChanged: () => void;
}) {
  const project = item.project_id ?? null;
  const action = primaryAction(item, project);
  const [panel, setPanel] = useState<'confirm' | 'answer' | 'release' | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const safety = isSafety(item);
  const origin = originLabel(item, sessions);
  const coalesced = coalescedLabel(item);
  const titleId = useId();
  const href = refHref(item, project);

  const run = (what: string, call: () => Promise<unknown>, done: string) => {
    setBusy(what);
    setError(null);
    call()
      .then(() => {
        toast.success(done);
        setPanel(null);
        onChanged();
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setBusy(null));
  };

  const primary = () => {
    switch (action.type) {
      case 'confirm_decision':
        return setPanel(panel === 'confirm' ? null : 'confirm');
      case 'answer':
        return setPanel(panel === 'answer' ? null : 'answer');
      case 'release_all':
        return setPanel(panel === 'release' ? null : 'release');
      case 'request_checkpoint':
        return run('primary', () => crewApi.requestCheckpoint(action.sessionId, 'Requested from the Needs-you inbox'), 'Checkpoint requested. The agent gets it at its next tool call.');
      case 'claim':
        return run('primary', () => crewApi.inboxItem(item.id, 'claim'), 'Yours. It shows as taken for the rest of the crew.');
      default:
        return undefined;
    }
  };

  return (
    <li
      aria-labelledby={titleId}
      className={clsx('relative px-4 py-3.5 sm:px-5', safety && 'bg-signal-wash/50')}
    >
      {safety && <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px] bg-signal" />}
      <div className="flex items-start gap-3">
        <span className={clsx('mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-[3px] border', safety ? 'border-signal text-ink' : 'border-rule text-ink-2')}>
          <PixelGlyph name={GLYPH[item.kind] ?? 'inbox'} size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-ink-3">
            <span className={clsx('uppercase tracking-[0.08em]', safety ? 'font-bold text-signal-ink' : 'text-ink-3')}>{kindLabel(item.kind)}</span>
            {safety && <Pill tone="open">safety</Pill>}
            {showProject && project && <span className="font-bold text-ink-2">{project}</span>}
            {origin && <span>{origin}</span>}
            {coalesced && <Pill>{coalesced}</Pill>}
            {item.state === 'claimed' && <Pill tone="ok">{takenBy(item.claimed_by, sessions)}</Pill>}
            <time dateTime={item.updated_at} title={absoluteTime(item.updated_at)} className="ml-auto">
              {relativeTime(item.updated_at, now)}
            </time>
          </p>
          <p id={titleId} className="mt-1 text-[15px] font-semibold leading-snug text-ink [overflow-wrap:anywhere]">
            {item.title}
          </p>
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {action.type === 'link' && (
              <a href={action.href} className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs">
                {action.label}
              </a>
            )}
            {action.type !== 'link' && action.type !== 'none' && (
              <button
                type="button"
                onClick={primary}
                disabled={busy !== null}
                aria-expanded={['confirm_decision', 'answer', 'release_all'].includes(action.type) ? panel !== null : undefined}
                className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-xs disabled:opacity-50"
              >
                {busy === 'primary' && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
                {action.label}
              </button>
            )}
            {action.type !== 'link' && href && (
              <a href={href} className="font-mono text-[11px] text-ink-3 underline decoration-rule underline-offset-2 hover:text-ink">
                open
              </a>
            )}
            {item.audience === 'project' && (
              <>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => run('resolve', () => crewApi.inboxItem(item.id, 'resolve'), 'Marked done')}
                  className="ml-auto rr-btn-ghost px-2.5 py-1 text-xs disabled:opacity-50"
                >
                  {busy === 'resolve' ? 'Saving…' : 'Done'}
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => run('dismiss', () => crewApi.inboxItem(item.id, 'dismiss'), 'Dismissed')}
                  className="rr-btn-ghost px-2.5 py-1 text-xs disabled:opacity-50"
                >
                  {busy === 'dismiss' ? 'Saving…' : 'Dismiss'}
                </button>
              </>
            )}
            {item.audience === 'crew' && item.state === 'claimed' && (
              <button
                type="button"
                disabled={busy !== null}
                onClick={() => run('resolve', () => crewApi.inboxItem(item.id, 'resolve'), 'Resolved')}
                className="ml-auto rr-btn-ghost px-2.5 py-1 text-xs disabled:opacity-50"
              >
                {busy === 'resolve' ? 'Saving…' : 'Resolve'}
              </button>
            )}
          </div>
          {panel === 'confirm' && (
            <div className="mt-3">
              <ProposedDecisions crewId={item.crew_id} state={state} sessions={sessions} onDone={onChanged} />
            </div>
          )}
          {panel === 'answer' && (
            <div className="mt-3">
              <Answer item={item} onDone={onChanged} />
            </div>
          )}
          {panel === 'release' && action.type === 'release_all' && (
            <form
              className="mt-3 space-y-2 border-l-[3px] border-fail bg-fail-wash px-3 py-2.5"
              onSubmit={(e) => {
                e.preventDefault();
                if (!reason.trim()) return;
                run('release', () => crewApi.releaseAllClaims(action.sessionId, reason.trim()), 'Released. The agent is told to claim again before editing.');
              }}
            >
              <label className="block text-sm text-ink" htmlFor={`${titleId}-reason`}>
                Release every claim this agent holds? It is stopped at its next tool call in those zones. Why?
              </label>
              <input
                id={`${titleId}-reason`}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                required
                maxLength={500}
                placeholder="Holding too much; others are waiting"
                className="rr-input w-full px-2.5 py-1.5 text-sm"
              />
              <div className="flex gap-2">
                <button type="submit" disabled={busy !== null || !reason.trim()} className="rr-btn-primary px-3 py-1.5 text-xs disabled:opacity-50">
                  {busy === 'release' ? 'Releasing…' : 'Release all claims'}
                </button>
                <button type="button" onClick={() => setPanel(null)} className="rr-btn-ghost px-3 py-1.5 text-xs">
                  Cancel
                </button>
              </div>
            </form>
          )}
          {error && (
            <p role="alert" className="mt-2 border-l-[3px] border-fail bg-fail-wash px-2.5 py-1.5 text-xs text-ink">
              {error}
            </p>
          )}
        </div>
      </div>
    </li>
  );
}
