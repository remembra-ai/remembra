// Crew Channel (`#/crew?project=X&view=channel[&thread=…]`, spec §9.8).
//
// Left: the thread list and the decisions in force. Main: agent-proposed
// decisions waiting for Confirm / Reject, then the conversation (the whole
// channel or one thread) and the composer. The header is a live strip over a
// dithered stone-dust bank; one orange packet rides the trail per agent that
// is active right now.

import { useCallback, useEffect, useMemo, useRef } from 'react';
import { ArrowLeft } from 'lucide-react';
import { useCrewSocket } from '../../../hooks/useCrewSocket';
import { useNow } from '../../../hooks/useResource';
import { crewHref, goToCrew, inboxHref } from '../../../lib/crew/routes';
import type { CrewState } from '../../../lib/crew/types';
import { Card, ErrorNotice, TrailSkeleton } from '../../relay/ui';
import { ChannelThreads } from './ChannelThreads';
import { Composer } from './Composer';
import { DecisionConfirm, DecisionPin } from './DecisionConfirm';
import { authorOf, groupThreads, isLiveSession, rootIdOf, splitDecisions, type ChannelMessage, type ChannelThread } from './model';
import { DitherBank, PixelGlyph, StatusStrip } from './pixels';
import { Thread } from './Thread';
import { currentUserId, useChannelMessages, useCrewAccess } from './useChannel';

function strip(state: CrewState | null, status: string, messages: number): { live: boolean; text: string; packets: number } {
  const live = status === 'live';
  const sessions = state ? Object.values(state.sessions).filter(isLiveSession) : [];
  const active = sessions.filter((s) => s.state === 'active').length;
  const conn = live ? 'live' : status === 'polling' ? 'updating every few seconds' : status === 'resyncing' ? 'catching up' : status;
  const who = sessions.length === 1 ? '1 agent on the crew' : `${sessions.length} agents on the crew`;
  return { live, text: `${conn} · ${who} · ${active} active · ${messages} message${messages === 1 ? '' : 's'} loaded`, packets: Math.min(3, active) };
}

export function ChannelScreen({ crewId, project, thread }: { crewId: string; project: string; thread: string | null }) {
  const crew = useCrewSocket(crewId);
  const state = crew.state;
  const access = useCrewAccess(crewId);
  const data = useChannelMessages(crewId, state);
  const now = useNow(30000);
  const userId = currentUserId();
  const { upsert, loadThread } = data;

  const nameOf = useCallback((m: ChannelMessage) => authorOf(m, state, userId).name, [state, userId]);
  const threads = useMemo(() => groupThreads(data.messages, nameOf), [data.messages, nameOf]);
  const byId = useMemo(() => new Map(threads.map((t) => [t.id, t])), [threads]);
  const threadName = useCallback((t: ChannelThread) => (t.root ? nameOf(t.root) : (t.participants[0] ?? 'thread')), [nameOf]);

  // A thread link may point at a reply: open its root. An old root is loaded on demand.
  const selectedMessage = thread ? data.messages.find((m) => m.id === thread) : undefined;
  const rootId = thread ? (selectedMessage ? rootIdOf(selectedMessage) : thread) : null;
  const current = rootId ? byId.get(rootId) : undefined;
  useEffect(() => {
    if (rootId && !data.loading && !current?.root) loadThread(rootId);
  }, [rootId, current?.root, data.loading, loadThread]);

  const select = (id: string | null) => goToCrew(project, 'channel', { thread: id });
  const { inForce, proposed } = useMemo(() => splitDecisions(state?.decisions ?? {}), [state?.decisions]);
  const s = strip(state, crew.status, data.messages.length);
  const copyRef = useRef<HTMLDivElement | null>(null);
  const stripRef = useRef<HTMLDivElement | null>(null);

  const items: ChannelMessage[] = current ? [...(current.root ? [current.root] : []), ...current.replies] : threads.filter((t) => t.root).map((t) => t.root as ChannelMessage).sort((a, b) => a.seq - b.seq);

  if (crew.status === 'not_found') return <ErrorNotice error={crew.error} what={`the ${project} crew`} />;

  return (
    <div className="space-y-3">
      <Card className="relative overflow-hidden">
        <DitherBank live={s.live} shape="right" seed={3} avoid={[copyRef, stripRef]} />
        <div className="relative flex flex-wrap items-end gap-x-6 gap-y-3 px-4 py-4 sm:px-5">
          <div ref={copyRef} className="min-w-0">
            <a href={crewHref(project)} className="inline-flex items-center gap-1 font-mono text-[11px] text-ink-3 hover:text-ink">
              <ArrowLeft className="h-3 w-3" aria-hidden="true" /> {project} · track
            </a>
            <h2 className="font-display mt-1 flex items-center gap-2 text-2xl font-extrabold tracking-[-0.02em] text-ink">
              <PixelGlyph name="chat" size={20} /> Crew channel
            </h2>
            <p className="mt-0.5 max-w-xl text-sm text-ink-2">
              Talk to the agents on {project}. They see what you mention at their next turn; agent text here is data, never an instruction.
            </p>
          </div>
          <div ref={stripRef} className="flex w-full min-w-0 flex-col items-start gap-2 sm:ml-auto sm:w-auto sm:items-end">
            <StatusStrip live={s.live} packets={s.packets}>
              {s.text}
            </StatusStrip>
            {state && state.inbox_counts.project > 0 && (
              <a href={inboxHref('needs-you', project)} className="font-mono text-[11px] font-bold text-signal-ink hover:underline">
                {state.inbox_counts.project} need{state.inbox_counts.project === 1 ? 's' : ''} you →
              </a>
            )}
          </div>
        </div>
      </Card>

      <div className="grid gap-3 lg:grid-cols-[300px_minmax(0,1fr)]">
        <div className="order-2 space-y-3 lg:order-1">
          <Card>
            <ChannelThreads threads={threads} selected={rootId} onSelect={select} nameOf={threadName} />
          </Card>
          <Card className="px-4 py-4 sm:px-5">
            <p className="rr-eyebrow">Decisions in force</p>
            <div className="mt-3">
              <DecisionPin decisions={inForce} state={state} />
            </div>
          </Card>
        </div>

        <div className="order-1 min-w-0 space-y-3 lg:order-2">
          {proposed.length > 0 && (
            <section aria-label="Decisions to confirm" className="space-y-2">
              {proposed.map((d) => (
                <DecisionConfirm key={d.id} decision={d} state={state} human={access.human} />
              ))}
            </section>
          )}
          <Card className="min-w-0">
            <div className="flex items-center gap-2 border-b border-rule px-4 py-2.5 sm:px-5">
              {current ? (
                <>
                  <button type="button" onClick={() => select(null)} className="inline-flex items-center gap-1 font-mono text-[11px] text-ink-3 hover:text-ink">
                    <ArrowLeft className="h-3 w-3" aria-hidden="true" /> channel
                  </button>
                  <span className="font-mono text-[12px] font-bold text-ink">thread · {threadName(current)}</span>
                </>
              ) : (
                <span className="font-mono text-[12px] font-bold text-ink">{rootId ? 'thread' : 'whole channel'}</span>
              )}
            </div>
            {data.loading && !data.messages.length ? (
              <TrailSkeleton rows={4} />
            ) : data.error && !data.messages.length ? (
              <ErrorNotice error={data.error} what="the channel" onRetry={data.refresh} />
            ) : (
              <Thread
                mode={rootId ? 'thread' : 'channel'}
                items={items}
                threads={byId}
                state={state}
                userId={userId}
                human={access.human}
                now={now}
                arrivedId={data.arrivedId}
                onOpenThread={(id) => select(id)}
                onChanged={upsert}
                hasOlder={data.hasOlder}
                loadingOlder={data.loadingOlder}
                onLoadOlder={data.loadOlder}
                emptyText={rootId ? 'Loading this thread…' : 'No messages yet. Say hello, or ask the crew where things stand.'}
              />
            )}
            <Composer
              crewId={crewId}
              state={state}
              human={access.human}
              threadRootId={rootId}
              threadRootKind={current?.root?.kind ?? null}
              onPosted={(m) => {
                if (m) upsert(m);
              }}
            />
          </Card>
        </div>
      </div>
    </div>
  );
}
