// The drawer that opens when a Constellation node is clicked: an agent's or
// project's trail, a single handoff, or an entity with its relationships and
// the memories that mention it.

import { useEffect, useRef } from 'react';
import { ArrowRight, X } from 'lucide-react';
import { api } from '../lib/api';
import type { InboxMessage, TrailItem } from '../lib/relay';
import { agentMeta, canonicalAgentId } from '../lib/agents';
import { hrefFor } from '../lib/nav';
import { relativeTime } from '../lib/time';
import { useResource } from '../hooks/useResource';
import { AgentAvatar, ErrorNotice, Pill, Skeleton } from '../components/relay/ui';
import type { GNode } from './model';

function TrailList({ items, now, showAgent }: { items: TrailItem[]; now: Date; showAgent: boolean }) {
  if (!items.length) return <p className="text-sm text-ink-3">No trail entries in the loaded window.</p>;
  return (
    <ol className="divide-y divide-rule border-y border-rule">
      {items.slice(0, 8).map((item) => (
        <li key={item.id}>
          <a href={hrefFor('trail', { open: item.id, project: item.project_id })} className="block py-2 hover:bg-paper-2">
            <span className="flex items-center gap-2">
              {showAgent && <AgentAvatar agentId={item.agent_id} size="sm" />}
              <span className="min-w-0 flex-1 truncate text-sm text-ink">{item.headline || item.memory_type}</span>
            </span>
            <span className="mt-0.5 flex flex-wrap gap-x-2 font-mono text-[11px] text-ink-3">
              <span>{item.memory_type}</span>
              <span>{relativeTime(item.created_at, now)}</span>
              {!showAgent && item.project_id && <span>{item.project_id}</span>}
              {item.failing > 0 && <span className="text-fail">{item.failing} failing</span>}
            </span>
          </a>
        </li>
      ))}
    </ol>
  );
}

function EntityDetail({ node }: { node: GNode }) {
  const details = useResource(`constellation-entity:${node.ref}`, () =>
    Promise.all([api.getEntityRelationships(node.ref), api.getEntityMemories(node.ref, 8)]),
  );
  if (details.loading) {
    return (
      <div className="space-y-2" role="status" aria-label="Loading entity">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-16 w-full" />
      </div>
    );
  }
  if (!details.data) return <ErrorNotice compact error={details.error} what="this entity" onRetry={details.refresh} />;
  const [rels, mems] = details.data;
  return (
    <div className="space-y-4">
      <section>
        <h3 className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Relationships</h3>
        {rels.relationships.length === 0 ? (
          <p className="mt-1 text-sm text-ink-3">None resolved yet.</p>
        ) : (
          <ul className="mt-1 space-y-1 text-sm">
            {rels.relationships.slice(0, 10).map((r) => (
              <li key={r.id} className="text-ink-2">
                <span className="text-ink">{r.from_entity_name}</span> <span className="font-mono text-[11px] text-ink-3">{r.type}</span>{' '}
                <span className="text-ink">{r.to_entity_name}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h3 className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">In {mems.total} memories</h3>
        <ul className="mt-1 divide-y divide-rule border-y border-rule">
          {mems.memories.map((m) => (
            <li key={m.id} className="py-2 text-sm text-ink-2">
              <p className="line-clamp-3">{m.content}</p>
            </li>
          ))}
        </ul>
      </section>
      <a
        href={hrefFor('entities')}
        className="inline-flex items-center gap-1 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4"
      >
        All entities <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
      </a>
    </div>
  );
}

export function NodeDrawer({
  node,
  trail,
  messages,
  now,
  onClose,
}: {
  node: GNode;
  trail: TrailItem[];
  messages: InboxMessage[];
  now: Date;
  onClose: () => void;
}) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, [node.id]);

  let body: React.ReactNode = null;
  let eyebrow: string = node.kind;
  let title = node.label;

  if (node.kind === 'agent') {
    const meta = agentMeta(node.ref);
    const id = canonicalAgentId(node.ref);
    const items = trail.filter((t) => canonicalAgentId(t.agent_id) === id);
    const notes = messages.filter((m) => canonicalAgentId(m.from_agent) === id || canonicalAgentId(m.to_agent) === id).slice(0, 5);
    title = node.ref === 'dashboard' ? 'You (dashboard)' : meta.name;
    eyebrow = node.lastAt ? `agent · active ${relativeTime(new Date(node.lastAt), now)}` : 'agent';
    body = (
      <div className="space-y-4">
        <TrailList items={items} now={now} showAgent={false} />
        {notes.length > 0 && (
          <section>
            <h3 className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Inbox</h3>
            <ul className="mt-1 space-y-1 text-sm text-ink-2">
              {notes.map((m) => (
                <li key={m.inbox_id}>
                  <a href={hrefFor('inbox', { open: m.inbox_id })} className="hover:text-ink">
                    <span className="font-mono text-[11px] text-ink-3">
                      {agentMeta(m.from_agent).name} → {agentMeta(m.to_agent).name}
                    </span>{' '}
                    {m.subject}
                  </a>
                </li>
              ))}
            </ul>
          </section>
        )}
        <div className="flex flex-wrap gap-2">
          <a href={hrefFor('trail', { agent: node.ref })} className="rr-btn-ghost inline-flex items-center gap-1 px-3 py-1.5 text-sm">
            Agent trail <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
          {node.ref !== 'dashboard' && (
            <a href={hrefFor('inbox', { compose: '1', to: node.ref })} className="rr-btn-primary inline-flex items-center px-3 py-1.5 text-sm">
              Write to {meta.name}
            </a>
          )}
        </div>
      </div>
    );
  } else if (node.kind === 'project') {
    const items = trail.filter((t) => (t.project_id || 'default') === node.ref);
    eyebrow = node.lastAt ? `project · active ${relativeTime(new Date(node.lastAt), now)}` : 'project';
    body = (
      <div className="space-y-4">
        <TrailList items={items} now={now} showAgent />
        <a href={hrefFor('trail', { project: node.ref })} className="rr-btn-ghost inline-flex items-center gap-1 px-3 py-1.5 text-sm">
          Project trail <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      </div>
    );
  } else if (node.kind === 'handoff' || node.kind === 'checkpoint') {
    const item = trail.find((t) => t.id === node.ref);
    eyebrow = `${node.kind} · ${node.lastAt ? relativeTime(new Date(node.lastAt), now) : ''}`;
    body = item ? (
      <div className="space-y-3">
        <p className="flex items-center gap-2 text-sm text-ink-2">
          <AgentAvatar agentId={item.agent_id} size="sm" /> {agentMeta(item.agent_id).name} in {item.project_id || 'default'}
        </p>
        <div className="flex flex-wrap gap-1.5">
          {item.branch && <Pill>{item.branch}</Pill>}
          {item.failing > 0 && <Pill tone="fail">{item.failing} failing</Pill>}
          {item.open > 0 && <Pill tone="open">{item.open} open</Pill>}
        </div>
        <a
          href={hrefFor('trail', { open: item.id, project: item.project_id })}
          className="rr-btn-primary inline-flex items-center gap-1 px-3 py-1.5 text-sm"
        >
          Open on the trail <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      </div>
    ) : (
      <p className="text-sm text-ink-3">This entry is no longer in the loaded trail.</p>
    );
  } else {
    eyebrow = `entity · ${node.detail ?? 'unknown type'}${node.project ? ` · ${node.project}` : ''}`;
    body = <EntityDetail node={node} />;
  }

  return (
    <aside
      ref={ref}
      tabIndex={-1}
      aria-label={`${node.kind}: ${title}`}
      className="modal-surface absolute inset-x-2 bottom-2 z-10 max-h-[70%] overflow-y-auto rounded-[3px] outline-none md:inset-x-auto md:bottom-3 md:right-3 md:top-3 md:max-h-none md:w-[340px]"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose();
      }}
    >
      <div className="sticky top-0 flex items-start justify-between gap-3 border-b border-rule bg-panel px-4 py-3">
        <div className="min-w-0">
          <p className="rr-eyebrow">{eyebrow}</p>
          <h2 className="font-display mt-1 break-words text-lg font-bold leading-tight text-ink">{title}</h2>
        </div>
        <button type="button" onClick={onClose} className="rounded-[2px] p-1.5 text-ink-3 hover:bg-paper-2 hover:text-ink" aria-label="Close">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="px-4 py-3">{body}</div>
    </aside>
  );
}
