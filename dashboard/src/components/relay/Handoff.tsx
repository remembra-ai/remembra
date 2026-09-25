// A handoff's sections (Done / Not done / Failing / Next) and one node of the
// trail. Shared by Home (latest handoff, "since you last looked") and Trail.

import { useId, type ReactNode } from 'react';
import clsx from 'clsx';
import { ChevronDown, GitBranch } from 'lucide-react';
import type { TrailDetail, TrailItem } from '../../lib/relay';
import { absoluteTime, relativeTime, shortSha, where } from '../../lib/time';
import { agentMeta } from '../../lib/agents';
import { AgentAvatar, AgentName, Pill } from './ui';

function stripPrefix(text: string, prefix: RegExp): string {
  return text.replace(prefix, '').trim();
}

function SectionList({
  heading,
  items,
  mark,
  tone,
  max,
}: {
  heading: string;
  items: string[];
  mark: string;
  tone: 'ok' | 'open' | 'fail';
  max?: number;
}) {
  if (items.length === 0) return null;
  const shown = max ? items.slice(0, max) : items;
  const hidden = items.length - shown.length;
  return (
    <div>
      <h4 className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-ink-3">{heading}</h4>
      <ul className="mt-1 space-y-0.5">
        {shown.map((item, index) => (
          <li key={index} className="grid grid-cols-[1.4ch_minmax(0,1fr)] gap-2 text-sm leading-snug text-ink">
            <span
              aria-hidden="true"
              className={clsx(
                'font-mono font-bold',
                tone === 'ok' && 'text-ok',
                tone === 'open' && 'text-signal-ink',
                tone === 'fail' && 'text-fail',
              )}
            >
              {mark}
            </span>
            <span className="break-words [overflow-wrap:anywhere]">{item}</span>
          </li>
        ))}
      </ul>
      {hidden > 0 && <p className="mt-1 pl-[calc(1.4ch+0.5rem)] font-mono text-[11px] text-ink-3">+{hidden} more</p>}
    </div>
  );
}

/** Done / Not done / Failing / Next for a structured handoff; the text for anything else. */
export function HandoffSections({ detail, max, className }: { detail: TrailDetail; max?: number; className?: string }) {
  if (!detail.structured) {
    return (
      <p className={clsx('whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-2', className)}>
        {detail.content || 'No text was stored with this entry.'}
      </p>
    );
  }
  const done = detail.done;
  const notDone = detail.not_done.map((t) => stripPrefix(t, /^TODO:\s*/i));
  const failing = detail.failing.map((t) => stripPrefix(t, /^FAILING:\s*/i));
  const empty = !done.length && !notDone.length && !failing.length && !detail.next;
  return (
    <div className={clsx('space-y-3', className)}>
      <SectionList heading="Done" items={done} mark={'✓'} tone="ok" max={max} />
      <SectionList heading="Not done / open" items={notDone} mark={'→'} tone="open" max={max} />
      <SectionList heading="Failing" items={failing} mark={'✕'} tone="fail" max={max} />
      {detail.next && (
        <div>
          <h4 className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-signal-ink">Next step</h4>
          <p className="mt-1 text-sm font-semibold leading-snug text-ink [overflow-wrap:anywhere]">{detail.next}</p>
        </div>
      )}
      {empty && <p className="text-sm text-ink-3">The session closed without commits, tests or open items.</p>}
    </div>
  );
}

export function BranchLabel({ branch, sha, className }: { branch: string | null; sha: string | null; className?: string }) {
  const text = where(branch, sha);
  if (!text) return null;
  return (
    <span
      className={clsx('inline-flex min-w-0 items-center gap-1 font-mono text-[11px] text-ink-2', className)}
      title={sha ? `${branch ?? ''} ${sha}` : undefined}
    >
      <GitBranch className="h-3 w-3 shrink-0 text-ink-3" aria-hidden="true" />
      <span className="truncate">{text}</span>
    </span>
  );
}

function CountPills({ item }: { item: TrailItem }) {
  return (
    <>
      {item.failing > 0 && <Pill tone="fail">{item.failing} failing</Pill>}
      {item.open > 0 && <Pill tone="open">{item.open} open</Pill>}
      {item.memory_type === 'checkpoint' && <Pill>checkpoint</Pill>}
    </>
  );
}

/** Details below an expanded trail node: sections, commits, and session facts. */
export function HandoffDetail({ item }: { item: TrailItem }) {
  const detail = item.detail;
  if (!detail) {
    return <p className="text-sm text-ink-3">This server does not send handoff details yet. Update Remembra to see them here.</p>;
  }
  const facts: ReactNode[] = [];
  if (detail.structured) {
    if (detail.unpushed_commits) facts.push(`${detail.unpushed_commits} unpushed to ${detail.upstream || 'upstream'}`);
    if (detail.files_changed_count) facts.push(`${detail.files_changed_count} files changed`);
    if (detail.uncommitted_count) facts.push(`${detail.uncommitted_count} uncommitted`);
    if (detail.end_reason) facts.push(`ended: ${detail.end_reason}`);
    if (detail.grounding_status === 'contradicted') facts.push('summary contradicted by the facts');
    if (detail.grounding_status === 'consistent') facts.push('summary checked against the facts');
  }
  if (item.session_id) facts.push(`session ${item.session_id.slice(0, 40)}`);
  return (
    <div className="space-y-4">
      <HandoffSections detail={detail} />
      {detail.structured && detail.commits.length > 0 && (
        <div>
          <h4 className="font-mono text-[11px] font-bold uppercase tracking-[0.08em] text-ink-3">Commits</h4>
          <ul className="mt-1 space-y-0.5">
            {detail.commits.map((commit, index) => (
              <li key={`${commit.sha}-${index}`} className="flex gap-2 text-sm">
                <code className="shrink-0 font-mono text-xs leading-5 text-signal-ink">{shortSha(commit.sha)}</code>
                <span className="min-w-0 break-words text-ink-2">{commit.subject || '(no subject)'}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {facts.length > 0 && (
        <p className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-ink-3">
          {facts.map((fact, index) => (
            <span key={index}>{fact}</span>
          ))}
        </p>
      )}
    </div>
  );
}

/**
 * One trail node. The parent draws the dashed rail; this renders the marker
 * on it and the entry, expandable to the full handoff.
 */
export function TrailNode({
  item,
  latest,
  showProject,
  expanded,
  onToggle,
  now,
}: {
  item: TrailItem;
  latest?: boolean;
  showProject?: boolean;
  expanded: boolean;
  onToggle: () => void;
  now: Date;
}) {
  const panelId = useId();
  const meta = agentMeta(item.agent_id);
  const checkpoint = item.memory_type === 'checkpoint';
  return (
    <li className="relative grid grid-cols-[32px_minmax(0,1fr)] gap-x-3">
      <span className="relative flex justify-center pt-4" aria-hidden="true">
        <span
          className={clsx(
            'relative z-[1] block',
            checkpoint
              ? 'mt-0.5 h-2.5 w-2.5 rotate-45 border-2 border-signal bg-panel'
              : latest
                ? 'h-3 w-3 rounded-full border-2 border-signal bg-signal shadow-[0_0_0_4px_var(--signal-wash)]'
                : 'h-3 w-3 rounded-full border-2 border-ink-3 bg-panel',
          )}
        />
      </span>
      <div className="min-w-0 border-b border-rule py-3 last:border-b-0">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-controls={panelId}
          className="group flex w-full min-w-0 items-start gap-3 rounded-[2px] text-left"
        >
          <AgentAvatar agentId={item.agent_id} size="md" />
          <span className="min-w-0 flex-1">
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <AgentName agentId={item.agent_id} className="text-[15px]" />
              <time
                dateTime={item.created_at}
                title={absoluteTime(item.created_at)}
                className="font-mono text-[11px] text-ink-3"
              >
                {relativeTime(item.created_at, now)}
              </time>
              {latest && <Pill tone="signal">latest</Pill>}
            </span>
            <span className="mt-0.5 block text-sm leading-snug text-ink-2 [overflow-wrap:anywhere]">
              {item.headline || (checkpoint ? 'Checkpoint' : 'Handoff')}
            </span>
            <span className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <BranchLabel branch={item.branch} sha={item.head_commit} className="mr-1 max-w-full" />
              {showProject && item.project_id && <Pill>{item.project_id}</Pill>}
              <CountPills item={item} />
            </span>
          </span>
          <ChevronDown
            aria-hidden="true"
            className={clsx(
              'mt-1 h-4 w-4 shrink-0 text-ink-3 transition-transform group-hover:text-ink',
              expanded && 'rotate-180',
            )}
          />
          <span className="sr-only">
            {expanded ? 'Hide' : 'Show'} the full handoff from {meta.name}
          </span>
        </button>
        {expanded && (
          <div id={panelId} className="mt-3 border-l-[3px] border-signal bg-paper px-3 py-3 sm:ml-11">
            <HandoffDetail item={item} />
          </div>
        )}
      </div>
    </li>
  );
}
