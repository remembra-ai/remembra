// The Site Board (#/crews, spec §9.2): every project with agents on it as a
// mono build tree, needs-you first, then live, then idle. Counts stay live
// over the crew.summary socket topic; each card's tree (who is in which
// phase, what they hold, which batons wait) comes from the crew's snapshot,
// refetched (ETag, usually a 304) whenever the card's counts move.

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';
import { useNow } from '../../hooks/useResource';
import { agentMeta } from '../../lib/agents';
import { crewApi } from '../../lib/crew/api';
import { useCrewList } from '../../lib/crew/hooks';
import { crewHref, inboxHref } from '../../lib/crew/routes';
import type { CrewListItem, CrewSnapshot } from '../../lib/crew/types';
import { absoluteTime, relativeTime } from '../../lib/time';
import { ErrorNotice, StaleNotice, TrailSkeleton } from '../../components/relay/ui';
import { NoCrewsEmpty } from '../../components/crew/empty/EmptyStates';
import { DitherField } from '../../components/crew/lane/DitherField';
import { agentPageHref } from '../../components/crew/lane/model';
import { branch, buildTree, progressRail, type PhaseNode, type TreeLeaf } from './buildTree';
import { liveSplit, liveSplitText } from '../../lib/crew/selectors';

/** Cards whose trees are loaded (the rest show list data only). */
const TREE_LIMIT = 24;

interface SnapEntry {
  sig: string;
  etag: string | null;
  snapshot: CrewSnapshot | null;
}

function signature(item: CrewListItem): string {
  return [item.last_event_at ?? '', item.live, item.needs_you, item.crew_inbox, item.moments_24h].join('|');
}

/** Snapshots of the listed crews, refetched when a crew's counts or last event change. */
function useSiteSnapshots(items: CrewListItem[]): Map<string, CrewSnapshot | null> {
  const [entries, setEntries] = useState<Map<string, SnapEntry>>(() => new Map());
  const inflight = useRef(new Set<string>());
  const mounted = useRef(true);
  const wanted = useMemo(() => items.slice(0, TREE_LIMIT).map((i) => ({ id: i.crew.id, sig: signature(i) })), [items]);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    for (const { id, sig } of wanted) {
      const current = entries.get(id);
      if ((current && current.sig === sig) || inflight.current.has(id)) continue;
      inflight.current.add(id);
      // A result is kept even if the counts moved meanwhile: the entry carries the signature it
      // was fetched for, so the next pass refetches (ETag, usually a 304) when that is stale.
      void crewApi
        .snapshot(id, current?.etag ?? null)
        .then(
          (res) => ({ etag: res.etag, snapshot: res.data }),
          () => ({ etag: null, snapshot: null }),
        )
        .then(({ etag, snapshot }) => {
          inflight.current.delete(id);
          if (!mounted.current) return;
          setEntries((prev) => {
            const before = prev.get(id);
            return new Map(prev).set(id, { sig, etag: etag ?? before?.etag ?? null, snapshot: snapshot ?? before?.snapshot ?? null });
          });
        });
    }
  }, [wanted, entries]);

  return useMemo(() => {
    const out = new Map<string, CrewSnapshot | null>();
    for (const [id, e] of entries) out.set(id, e.snapshot);
    return out;
  }, [entries]);
}

function LeafRow({ leaf, gutter, last, project }: { leaf: TreeLeaf; gutter: string; last: boolean; project: string }) {
  const prefix = `${gutter}${branch(last)} `;
  if (leaf.kind === 'baton') {
    return (
      <li role="treeitem" aria-level={gutter ? 3 : 2} aria-label={leaf.status} className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span aria-hidden="true" className="whitespace-pre text-ink-3">
          {prefix}
        </span>
        <span aria-hidden="true" className="text-signal">
          {leaf.glyph}
        </span>
        <a href={crewHref(project)} className="font-semibold text-ink hover:underline">
          {leaf.slot.zones.map((z) => z.slug.toUpperCase()).join(', ')}
        </a>
        <span className="text-ink-3">{leaf.slot.taskRef ?? ''}</span>
        <span className="ml-auto min-w-0 truncate text-signal-ink">{leaf.right}</span>
      </li>
    );
  }
  const s = leaf.session;
  return (
    <li
      role="treeitem"
      aria-level={gutter ? 3 : 2}
      aria-label={`${s.callsign}${leaf.parentCallsign ? `, sub-agent of ${leaf.parentCallsign}` : ''}, ${leaf.status}${leaf.taskRef ? `, ${leaf.taskRef}` : ''}`}
      className="flex min-w-0 flex-wrap items-baseline gap-x-2"
    >
      <span aria-hidden="true" className="whitespace-pre text-ink-3">
        {prefix}
      </span>
      <span aria-hidden="true" className={clsx(leaf.alarm ? 'text-signal-ink' : leaf.glyph === '◉' ? 'text-signal' : 'text-ink-2')}>
        {leaf.glyph}
      </span>
      <span
        aria-hidden="true"
        className="rounded-[2px] px-1 text-[10px] font-bold text-white"
        style={{ background: agentMeta(s.agent_id).lane }}
      >
        {leaf.monogram}
      </span>
      <a href={agentPageHref(s.agent_id, s.id, project)} className="font-semibold text-ink hover:underline">
        {s.callsign}
      </a>
      {leaf.parentCallsign && <span className="text-[11px] text-ink-3">sub-agent of {leaf.parentCallsign}</span>}
      <span className="min-w-0 max-w-[22ch] truncate text-ink-2">{leaf.taskTitle ?? (leaf.taskRef ? leaf.taskRef : '—')}</span>
      <span className={clsx('ml-auto min-w-0 truncate', leaf.alarm ? 'text-signal-ink' : 'text-ink-3')}>{leaf.right || leaf.status}</span>
    </li>
  );
}

function PhaseRow({ node, last, project }: { node: PhaseNode; last: boolean; project: string }) {
  const gutter = last ? '   ' : '│  ';
  return (
    <li
      role="treeitem"
      aria-level={1}
      aria-expanded={node.children.length ? true : undefined}
      aria-label={`${node.label}, ${node.status}, ${node.done} of ${node.total} done`}
    >
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span aria-hidden="true" className="whitespace-pre text-ink-3">
          {branch(last)}{' '}
        </span>
        <span aria-hidden="true" className={clsx(node.glyph === '◉' ? 'text-signal' : node.glyph === '✓' ? 'text-ok' : 'text-ink-3')}>
          {node.glyph}
        </span>
        <span className="min-w-0 truncate font-semibold text-ink">{node.label}</span>
        <span className="ml-auto inline-flex items-baseline gap-2">
          <span aria-hidden="true" className="tracking-[-0.12em] text-ink-2">
            {progressRail(node.done, node.total, 10)}
          </span>
          <span className="tabular text-ink-2">
            {node.done}/{node.total}
          </span>
        </span>
      </div>
      {node.children.length > 0 && (
        <ul role="group" className="space-y-0.5 pt-0.5">
          {node.children.map((leaf, i) => (
            <LeafRow key={leaf.key} leaf={leaf} gutter={gutter} last={i === node.children.length - 1} project={project} />
          ))}
        </ul>
      )}
    </li>
  );
}

function ProjectCard({ item, snapshot, now }: { item: CrewListItem; snapshot: CrewSnapshot | null; now: Date }) {
  const titleId = useId();
  const tree = buildTree(item, snapshot, now.getTime());
  const project = item.crew.project_id;
  const rows = [...tree.phases];
  const looseLast = tree.loose.length > 0;
  return (
    <article aria-labelledby={titleId} className="rr-card mb-3 break-inside-avoid rounded-[3px]">
      <div className="px-4 pb-2 pt-4 sm:px-5">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <h2 id={titleId} className="font-display min-w-0 truncate text-xl font-extrabold uppercase tracking-[-0.02em] text-ink">
            <a href={crewHref(project)} className="hover:underline">
              {item.crew.name || project}
            </a>
          </h2>
          <p className="flex items-center gap-2 font-mono text-[12px]">
            <span className={clsx('inline-flex items-center gap-1.5', liveSplit(item).running > 0 ? 'text-ink' : 'text-ink-3')}>
              <span aria-hidden="true" className={clsx('h-2 w-2', liveSplit(item).running > 0 ? 'rr-pulse bg-signal' : 'bg-rule')} />
              {liveSplitText(liveSplit(item))}
            </span>
            {item.needs_you > 0 && (
              <a
                href={inboxHref('needs-you', project)}
                className="font-bold text-signal-ink underline decoration-signal decoration-2 underline-offset-2"
              >
                ⚑ {item.needs_you} need{item.needs_you === 1 ? 's' : ''} you
              </a>
            )}
          </p>
        </div>
        <p className="mt-0.5 flex flex-wrap justify-between gap-x-3 font-mono text-[11px] text-ink-3">
          <span className="truncate">{project}</span>
          <span title={absoluteTime(item.last_event_at)}>
            {item.last_event_at ? `last move ${relativeTime(item.last_event_at, now)}` : 'no moves yet'}
          </span>
        </p>
      </div>
      <div className="border-t border-dashed border-rule px-4 py-3 font-mono text-[12px] leading-relaxed sm:px-5">
        {rows.length === 0 && tree.loose.length === 0 ? (
          <p className="text-ink-3">No tasks and nobody on site. Tasks only close with a report.</p>
        ) : (
          <ul role="tree" aria-label={`${project} build tree`} className="space-y-0.5">
            {rows.map((node, i) => (
              <PhaseRow key={node.key} node={node} last={!looseLast && i === rows.length - 1} project={project} />
            ))}
            {looseLast && (
              <li role="treeitem" aria-level={1} aria-expanded aria-label="On site without a task">
                <div className="flex items-baseline gap-2">
                  <span aria-hidden="true" className="whitespace-pre text-ink-3">
                    {branch(true)}{' '}
                  </span>
                  <span className="text-ink-3">on site, no phase</span>
                </div>
                <ul role="group" className="space-y-0.5 pt-0.5">
                  {tree.loose.map((leaf, i) => (
                    <LeafRow key={leaf.key} leaf={leaf} gutter="   " last={i === tree.loose.length - 1} project={project} />
                  ))}
                </ul>
              </li>
            )}
          </ul>
        )}
        {tree.partial && item.live > 0 && <p className="mt-2 text-[11px] text-ink-3">loading who holds what…</p>}
      </div>
    </article>
  );
}

export function SiteBoard() {
  const list = useCrewList();
  const now = useNow(15000);
  const snapshots = useSiteSnapshots(list.items);
  if (list.status === 'loading') return <TrailSkeleton rows={3} />;
  if (list.status === 'error' && list.items.length === 0) {
    return <ErrorNotice error={list.error} what="your crews" onRetry={list.refresh} />;
  }
  if (list.items.length === 0) return <NoCrewsEmpty />;
  const live = list.items.reduce((n, i) => n + liveSplit(i).running, 0);
  const busy = list.items.filter((i) => liveSplit(i).running > 0).length;
  return (
    <div className="space-y-3">
      <header className="rr-card relative overflow-hidden rounded-[3px]">
        <DitherField shape="right" seed={3} />
        <div className="relative px-4 py-4 sm:px-5">
          <p className="rr-eyebrow">Site board</p>
          <p className="font-display mt-1.5 text-[clamp(1.4rem,1rem+1.4vw,2rem)] font-extrabold leading-none tracking-[-0.03em] text-ink">
            {live} agent{live === 1 ? '' : 's'} on {busy} site{busy === 1 ? '' : 's'}
            <span className="text-signal">.</span>
          </p>
          <p className="mt-1.5 font-mono text-[12px] text-ink-2">
            {list.items.length} crew{list.items.length === 1 ? '' : 's'} ·{' '}
            {list.needsYou > 0 ? (
              <a
                href={inboxHref('needs-you')}
                className="font-bold text-signal-ink underline decoration-signal decoration-2 underline-offset-2"
              >
                ⚑ {list.needsYou} need{list.needsYou === 1 ? 's' : ''} you
              </a>
            ) : (
              'nothing needs you'
            )}{' '}
            · needs-you first, then live, then idle
          </p>
        </div>
      </header>
      {list.error && <StaleNotice error={list.error} what="your crews" />}
      <div className={clsx('gap-3', list.items.length > 1 ? 'columns-1 xl:columns-2' : 'columns-1')}>
        {list.items.map((item) => (
          <ProjectCard key={item.crew.id} item={item} snapshot={snapshots.get(item.crew.id) ?? null} now={now} />
        ))}
      </div>
    </div>
  );
}
