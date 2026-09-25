// Trail = "git log for agents": every handoff and checkpoint, newest first,
// on a dashed rail grouped by day. Filter by project and agent; expand any
// node to its full handoff.

import { useState, type ReactNode } from 'react';
import clsx from 'clsx';
import { Loader2, RefreshCw } from 'lucide-react';
import { useRelayData } from '../hooks/relayData';
import { useNow, useResource } from '../hooks/useResource';
import { relay, type TrailItem } from '../lib/relay';
import { INSTALL_COMMAND, agentMeta } from '../lib/agents';
import { navigate, useRoute } from '../lib/nav';
import { dayLabel, relativeTime } from '../lib/time';
import { AgentAvatar, CopyCommand, ErrorNotice, StaleNotice, TrailSkeleton } from '../components/relay/ui';
import { TrailNode } from '../components/relay/Handoff';

const PAGE = 30;

interface Older {
  key: string;
  items: TrailItem[];
  done: boolean;
}

function FilterChip({
  selected,
  onClick,
  children,
  count,
}: {
  selected: boolean;
  onClick: () => void;
  children: ReactNode;
  count?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={selected}
      className={clsx(
        'inline-flex shrink-0 items-center gap-1.5 rounded-[2px] border px-2.5 py-1.5 font-mono text-xs transition-colors',
        selected ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink hover:text-ink',
      )}
    >
      {children}
      {count !== undefined && <span className={clsx('tabular', selected ? 'opacity-70' : 'text-ink-3')}>{count}</span>}
    </button>
  );
}

function groupByDay(items: TrailItem[], now: Date): { label: string; items: TrailItem[] }[] {
  const groups: { label: string; items: TrailItem[] }[] = [];
  for (const item of items) {
    const label = dayLabel(item.created_at, now);
    const last = groups[groups.length - 1];
    if (last && last.label === label) last.items.push(item);
    else groups.push({ label, items: [item] });
  }
  return groups;
}

export function Trail() {
  const { summary } = useRelayData();
  const { params } = useRoute();
  const now = useNow(30000);
  const project = params.get('project') || null;
  const agent = params.get('agent') || null;
  const openParam = params.get('open');
  const key = `trail:${project ?? '*'}:${agent ?? '*'}`;

  const head = useResource(key, () => relay.trail({ projectId: project, agentId: agent, limit: PAGE }), { pollMs: 30000 });
  const [older, setOlder] = useState<Older>({ key, items: [], done: false });
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<unknown>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(openParam ? [openParam] : []));
  const [lastOpenParam, setLastOpenParam] = useState(openParam);
  if (openParam !== lastOpenParam) {
    setLastOpenParam(openParam);
    if (openParam) setExpanded((prev) => new Set(prev).add(openParam));
  }

  const olderItems = older.key === key ? older.items : [];
  const seen = new Set<string>();
  const items: TrailItem[] = [];
  for (const item of [...(head.data?.items ?? []), ...olderItems]) {
    if (!seen.has(item.id)) {
      seen.add(item.id);
      items.push(item);
    }
  }
  const total = head.data?.total ?? 0;
  const canLoadMore = !!head.data && items.length < total && !(older.key === key && older.done);
  const latestHandoffId = items.find((item) => item.memory_type === 'handoff')?.id;

  const setFilter = (next: { project?: string | null; agent?: string | null }) => {
    navigate('trail', {
      project: next.project !== undefined ? next.project : project,
      agent: next.agent !== undefined ? next.agent : agent,
    });
  };

  const loadMore = () => {
    setLoadingMore(true);
    setMoreError(null);
    const offset = (head.data?.items.length ?? 0) + olderItems.length;
    relay
      .trail({ projectId: project, agentId: agent, limit: PAGE, offset })
      .then((page) => {
        setOlder((prev) => ({
          key,
          items: [...(prev.key === key ? prev.items : []), ...page.items],
          done: page.items.length < PAGE,
        }));
      })
      .catch((err: unknown) => setMoreError(err))
      .finally(() => setLoadingMore(false));
  };

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const projects = summary.data?.projects ?? [];
  const agents = summary.data?.agents ?? [];
  const filtered = !!(project || agent);

  return (
    <div className="space-y-4">
      <div className="rr-card rounded-[3px] px-4 py-3 sm:px-5">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <p className="font-mono text-xs text-ink-3" aria-live="polite">
            {head.data
              ? `${total.toLocaleString()} ${total === 1 ? 'entry' : 'entries'}${
                  items[0] ? ` · newest ${relativeTime(items[0].created_at, now)}` : ''
                }`
              : 'Loading the trail…'}
          </p>
          <button
            type="button"
            onClick={head.refresh}
            className="ml-auto inline-flex items-center gap-1.5 font-mono text-xs text-ink-2 hover:text-ink"
            aria-label="Refresh the trail"
          >
            <RefreshCw className={clsx('h-3.5 w-3.5', head.refreshing && 'animate-spin')} aria-hidden="true" /> refresh
          </button>
        </div>
        <div className="mt-3 space-y-2">
          <div role="group" aria-label="Filter by project" className="scrollbar-hide -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-0.5">
            <FilterChip selected={!project} onClick={() => setFilter({ project: null })}>
              all projects
            </FilterChip>
            {projects.map((p) => (
              <FilterChip
                key={p.project_id}
                selected={project === p.project_id}
                onClick={() => setFilter({ project: p.project_id })}
                count={p.handoffs + p.checkpoints}
              >
                {p.project_id}
              </FilterChip>
            ))}
            {project && !projects.some((p) => p.project_id === project) && (
              <FilterChip selected onClick={() => setFilter({ project: null })}>
                {project}
              </FilterChip>
            )}
          </div>
          {agents.length > 0 && (
            <div role="group" aria-label="Filter by agent" className="scrollbar-hide -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-0.5">
              <FilterChip selected={!agent} onClick={() => setFilter({ agent: null })}>
                all agents
              </FilterChip>
              {agents.map((a) => (
                <FilterChip
                  key={a.agent_id}
                  selected={agent === a.agent_id}
                  onClick={() => setFilter({ agent: a.agent_id })}
                  count={a.handoffs + a.checkpoints}
                >
                  <AgentAvatar agentId={a.agent_id} size="sm" />
                  {agentMeta(a.agent_id).name}
                </FilterChip>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="rr-card rounded-[3px]">
        {head.loading && <TrailSkeleton rows={5} />}
        {!head.data && !head.loading && head.error != null && (
          <ErrorNotice error={head.error} what="the trail" onRetry={head.refresh} />
        )}
        {head.data && head.error != null && <StaleNotice error={head.error} what="the trail" />}

        {head.data && items.length === 0 && (
          <div className="px-4 py-10 text-center sm:px-8">
            <div className="rr-rail mx-auto h-12 w-[2px]" aria-hidden="true" />
            {filtered ? (
              <>
                <p className="font-display mt-3 text-xl font-bold text-ink">No entries match these filters.</p>
                <p className="mt-1 text-sm text-ink-2">
                  {agent ? `${agentMeta(agent).name} hasn't left a handoff` : 'No handoffs yet'}
                  {project ? ` in ${project}` : ''}.
                </p>
                <button
                  type="button"
                  onClick={() => navigate('trail')}
                  className="rr-btn-ghost mt-4 px-3 py-2 text-sm"
                >
                  Clear filters
                </button>
              </>
            ) : (
              <>
                <p className="font-display mt-3 text-xl font-bold text-ink">The trail starts with your first handoff.</p>
                <p className="mx-auto mt-1 max-w-md text-sm text-ink-2">
                  When an agent stops, <code className="font-mono text-[13px]">remembra-relay close</code> records what it did, what it
                  left open, what is failing and what comes next. Each one lands here, newest first.
                </p>
                <CopyCommand className="mx-auto mt-4 max-w-md text-left" command={INSTALL_COMMAND} label="Install command" />
              </>
            )}
          </div>
        )}

        {items.length > 0 &&
          groupByDay(items, now).map((group) => (
            <section key={group.label} aria-label={group.label}>
              <h3 className="sticky top-0 z-[2] border-b border-rule bg-panel/95 px-4 py-2 font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-ink-3 backdrop-blur sm:px-5">
                {group.label}
                <span className="ml-2 font-normal">{group.items.length}</span>
              </h3>
              <ol className="relative px-4 sm:px-5">
                <span aria-hidden="true" className="rr-rail absolute bottom-0 left-[31px] top-0 w-[2px] sm:left-[35px]" />
                {group.items.map((item) => (
                  <TrailNode
                    key={item.id}
                    item={item}
                    latest={item.id === latestHandoffId && !filtered}
                    showProject={!project}
                    now={now}
                    expanded={expanded.has(item.id)}
                    onToggle={() => toggle(item.id)}
                  />
                ))}
              </ol>
            </section>
          ))}

        {moreError != null && <ErrorNotice error={moreError} what="older entries" onRetry={loadMore} />}
        {canLoadMore && (
          <div className="border-t border-rule px-4 py-3 text-center sm:px-5">
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="rr-btn-ghost inline-flex items-center gap-2 px-4 py-2 text-sm"
            >
              {loadingMore && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              Older entries
              <span className="font-mono text-xs text-ink-3">{(total - items.length).toLocaleString()} more</span>
            </button>
          </div>
        )}
        {head.data && items.length > 0 && !canLoadMore && (
          <p className="border-t border-rule px-4 py-3 text-center font-mono text-[11px] text-ink-3">start of the trail</p>
        )}
      </div>
    </div>
  );
}
