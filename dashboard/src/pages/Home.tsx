// Home = mission control: what your agents did since you last looked, the
// last handoff with a "continue with" command, unread messages, the week in
// one line, plan usage, and (for new users) the connect checklist.

import { useEffect, useId, useState } from 'react';
import { ArrowRight, Inbox as InboxIcon, PenLine } from 'lucide-react';
import { useRelayData } from '../hooks/relayData';
import { useNow, useResource } from '../hooks/useResource';
import { relay, type TrailItem } from '../lib/relay';
import { agentMeta } from '../lib/agents';
import { hrefFor } from '../lib/nav';
import { parseServerTime, relativeTime, greeting } from '../lib/time';
import { Card, CardHeader, ErrorNotice, Skeleton, StaleNotice, TrailSkeleton, AgentAvatar } from '../components/relay/ui';
import { TrailNode } from '../components/relay/Handoff';
import { LatestHandoff } from '../components/relay/LatestHandoff';
import { ConnectChecklist, FirstHandoffCelebration, PlanMeter, WeeklyRecap } from '../components/relay/HomeCards';

const LAST_SEEN_KEY = 'remembra_home_last_seen';
const LAST_ACTIVE_KEY = 'remembra_home_last_active';
/** Coming back within this gap (a reload, a quick detour) continues the same visit. */
const VISIT_GAP_MS = 30 * 60 * 1000;
const CELEBRATED_KEY = 'remembra_first_handoff_celebrated';
const SAW_EMPTY_KEY = 'remembra_saw_empty_trail';
const HIDE_CONNECT_KEY = 'remembra_hide_connect_checklist';

function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage unavailable (private mode): these are conveniences only.
  }
}

function firstName(userName?: string): string {
  if (!userName) return '';
  const base = userName.includes('@') ? userName.split('@')[0] : userName.split(' ')[0];
  return base ? base[0].toUpperCase() + base.slice(1) : '';
}

function plural(n: number, word: string): string {
  return `${n} ${n === 1 ? word : `${word}s`}`;
}

export function Home({ userName }: { userName?: string }) {
  const { trail, summary, inbox, usage } = useRelayData();
  const now = useNow(30000);
  // The baseline for "since you last looked": when the previous visit ended.
  // A reload or a quick detour continues the current visit (same baseline).
  const [since] = useState(() => {
    const lastActive = parseServerTime(readStorage(LAST_ACTIVE_KEY));
    const baseline = readStorage(LAST_SEEN_KEY);
    if (lastActive && Date.now() - lastActive.getTime() < VISIT_GAP_MS) return baseline;
    const next = lastActive ? lastActive.toISOString() : baseline;
    if (next) writeStorage(LAST_SEEN_KEY, next);
    return next;
  });
  const [celebrated, setCelebrated] = useState(() => readStorage(CELEBRATED_KEY) === '1');
  const [sawEmpty] = useState(() => readStorage(SAW_EMPTY_KEY) === '1');
  const [hideConnect, setHideConnect] = useState(() => readStorage(HIDE_CONNECT_KEY) === '1');
  const [expanded, setExpanded] = useState<string | null>(null);
  const sinceId = useId();
  const inboxId = useId();

  const unread = useResource('home-unread', () => relay.inboxMessages({ status: 'unread', limit: 3 }), { pollMs: 30000 });

  // Remember when this visit was last active (page left, hidden or closed).
  useEffect(() => {
    const save = () => writeStorage(LAST_ACTIVE_KEY, new Date().toISOString());
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') save();
    };
    window.addEventListener('pagehide', save);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      save();
      window.removeEventListener('pagehide', save);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  const items = trail.data?.items ?? [];
  const handoffs = items.filter((item) => item.memory_type === 'handoff');
  const latest = handoffs[0];
  const previous = handoffs[1];
  const hasHandoffs = !!latest;
  const loaded = !!trail.data;

  // Count arrivals: a new newest handoff while this page is open.
  const latestId = latest?.id ?? null;
  const [seenLatest, setSeenLatest] = useState<string | null | undefined>(undefined);
  const [arrivals, setArrivals] = useState(0);
  if (loaded && latestId !== seenLatest) {
    if (seenLatest !== undefined && latestId !== null) setArrivals((n) => n + 1);
    setSeenLatest(latestId);
  }

  useEffect(() => {
    if (loaded && !hasHandoffs) writeStorage(SAW_EMPTY_KEY, '1');
  }, [loaded, hasHandoffs]);

  const totalHandoffs = summary.data?.total_handoffs ?? (hasHandoffs ? handoffs.length : 0);
  const celebrate = hasHandoffs && !celebrated && (sawEmpty || arrivals > 0 || totalHandoffs === 1) && totalHandoffs <= 3;

  const sinceDate = parseServerTime(since);
  const newItems: TrailItem[] = sinceDate
    ? items.filter((item) => {
        const at = parseServerTime(item.created_at);
        return !!at && at > sinceDate;
      })
    : [];
  const newAgents = new Set(newItems.map((item) => item.agent_id || ''));
  const newFailing = newItems.filter((item) => item.failing > 0).length;
  const recentList = (sinceDate && newItems.length ? newItems : items).slice(0, 5);

  const agents = summary.data?.agents ?? [];
  const connectedCount = agents.length;
  const showConnectSide = hasHandoffs && connectedCount < 2 && !hideConnect;
  const forYou = inbox.data?.agents.find((a) => a.agent_id === 'dashboard')?.unread ?? 0;

  let statusLine: string;
  if (!loaded) statusLine = '';
  else if (!hasHandoffs && items.length === 0) statusLine = 'Connect an agent and its first handoff lands here.';
  else if (sinceDate && newItems.length > 0) {
    statusLine = `Since you last looked (${relativeTime(sinceDate, now)}): ${plural(
      newItems.filter((i) => i.memory_type === 'handoff').length,
      'handoff',
    )} from ${plural(newAgents.size, 'agent')}${newFailing ? `, ${newFailing} with failing checks` : ''}.`;
  } else if (sinceDate) statusLine = `Nothing new since you last looked, ${relativeTime(sinceDate, now)}. Your agents are quiet.`;
  else statusLine = "Here's where your agents left off.";

  const name = firstName(userName);

  return (
    <div className="space-y-5">
      <header className="pt-1">
        <p className="rr-eyebrow">
          Mission control ·{' '}
          {now.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}
        </p>
        <h2 className="font-display mt-2 text-[clamp(1.75rem,1.2rem+2vw,2.6rem)] font-extrabold leading-[1.02] tracking-[-0.03em] text-ink">
          {greeting(now)}
          {name ? `, ${name}` : ''}
          <span className="text-signal">.</span>
        </h2>
        <div className="mt-2 min-h-[1.5rem] text-[15px] text-ink-2" aria-live="polite">
          {loaded ? statusLine : <Skeleton className="h-4 w-72 max-w-full" />}
        </div>
      </header>

      {celebrate && latest && (
        <FirstHandoffCelebration
          item={latest}
          onDismiss={() => {
            writeStorage(CELEBRATED_KEY, '1');
            setCelebrated(true);
          }}
        />
      )}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,340px)]">
        <div className="min-w-0 space-y-5">
          {trail.loading && (
            <div className="rr-card rounded-[3px]">
              <div className="h-9 bg-head" />
              <TrailSkeleton rows={2} />
            </div>
          )}
          {!loaded && !trail.loading && trail.error != null && (
            <div className="rr-card rounded-[3px]">
              <ErrorNotice error={trail.error} what="the trail" onRetry={trail.refresh} />
            </div>
          )}
          {latest && <LatestHandoff latest={latest} previous={previous} arrivals={arrivals} now={now} />}
          {loaded && !hasHandoffs && <ConnectChecklist agents={agents} now={now} />}

          {loaded && items.length > 0 && (
            <Card labelledBy={sinceId}>
              <CardHeader
                id={sinceId}
                eyebrow={sinceDate && newItems.length ? `${newItems.length} new` : 'Latest'}
                title={sinceDate && newItems.length ? 'Since you last looked' : 'Recent activity'}
                action={
                  <a
                    href={hrefFor('trail')}
                    className="inline-flex items-center gap-1 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4"
                  >
                    Trail <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                  </a>
                }
              />
              {trail.error != null && <StaleNotice error={trail.error} what="the trail" />}
              <ol className="relative px-4 pb-2 pt-1 sm:px-5">
                <span aria-hidden="true" className="rr-rail absolute bottom-6 left-[31px] top-6 w-[2px] sm:left-[35px]" />
                {recentList.map((item) => (
                  <TrailNode
                    key={item.id}
                    item={item}
                    latest={item.id === latest?.id}
                    showProject
                    now={now}
                    expanded={expanded === item.id}
                    onToggle={() => setExpanded(expanded === item.id ? null : item.id)}
                  />
                ))}
              </ol>
            </Card>
          )}
        </div>

        <aside className="min-w-0 space-y-5" aria-label="At a glance">
          <Card labelledBy={inboxId}>
            <CardHeader
              id={inboxId}
              eyebrow="Inbox"
              title={
                inbox.data ? (
                  <span className="flex items-baseline gap-2">
                    <span className="tabular text-3xl font-extrabold">{inbox.data.unread_total}</span>
                    <span className="text-base font-bold text-ink-2">unread</span>
                  </span>
                ) : (
                  'Messages'
                )
              }
              action={<InboxIcon className="mt-1 h-5 w-5 text-ink-3" aria-hidden="true" />}
            />
            <div className="px-4 pb-4 pt-2 sm:px-5">
              {inbox.loading && <Skeleton className="h-10 w-full" />}
              {!inbox.data && inbox.error != null && <ErrorNotice compact error={inbox.error} what="the inbox" onRetry={inbox.refresh} />}
              {inbox.data && (
                <>
                  <p className="text-sm text-ink-2">
                    {inbox.data.unread_total === 0
                      ? 'Nothing waiting. Leave an agent a note and it leads its next session brief.'
                      : `Waiting to be picked up across ${plural(
                          inbox.data.agents.filter((a) => a.unread > 0).length,
                          'agent',
                        )}.`}
                    {forYou > 0 && <span className="font-semibold text-signal-ink"> {forYou} for you.</span>}
                  </p>
                  {unread.data && unread.data.items.length > 0 && (
                    <ul className="mt-3 divide-y divide-rule border-y border-rule">
                      {unread.data.items.map((message) => (
                        <li key={message.inbox_id}>
                          <a
                            href={hrefFor('inbox', { open: message.inbox_id })}
                            className="flex items-center gap-2.5 py-2 hover:bg-paper"
                          >
                            <AgentAvatar agentId={message.to_agent} size="sm" />
                            <span className="min-w-0 flex-1">
                              <span className="block truncate text-sm font-semibold text-ink">{message.subject}</span>
                              <span className="block truncate font-mono text-[11px] text-ink-3">
                                {agentMeta(message.from_agent).name} → {agentMeta(message.to_agent).name} ·{' '}
                                {relativeTime(message.created_at, now)}
                              </span>
                            </span>
                          </a>
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <a href={hrefFor('inbox', { compose: '1' })} className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-2 text-sm">
                      <PenLine className="h-4 w-4" aria-hidden="true" /> Write to an agent
                    </a>
                    <a href={hrefFor('inbox')} className="rr-btn-ghost inline-flex items-center px-3 py-2 text-sm">
                      Open inbox
                    </a>
                  </div>
                </>
              )}
            </div>
          </Card>

          {summary.data && summary.data.total_handoffs + summary.data.total_checkpoints > 0 && (
            <WeeklyRecap summary={summary.data} now={now} />
          )}
          {!summary.data && summary.error != null && (
            <div className="rr-card rounded-[3px]">
              <ErrorNotice compact error={summary.error} what="this week's recap" onRetry={summary.refresh} />
            </div>
          )}

          {usage.data && <PlanMeter usage={usage.data} />}

          {showConnectSide && (
            <ConnectChecklist
              agents={agents}
              now={now}
              onDismiss={() => {
                writeStorage(HIDE_CONNECT_KEY, '1');
                setHideConnect(true);
              }}
            />
          )}
        </aside>
      </div>
    </div>
  );
}
