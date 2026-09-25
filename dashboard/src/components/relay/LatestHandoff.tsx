// The home page's centerpiece: the newest handoff, with the baton resting on
// the agent that holds it. When a new handoff arrives (polling), the baton
// travels down the dashed rail from the previous agent to the new one.

import { useEffect, useState } from 'react';
import { motion, useReducedMotion } from 'framer-motion';
import { ArrowRight, ClipboardCopy, ShieldCheck } from 'lucide-react';
import clsx from 'clsx';
import type { TrailItem } from '../../lib/relay';
import { agentMeta } from '../../lib/agents';
import { absoluteTime, relativeTime, where } from '../../lib/time';
import { continueCommand, continuePrompt } from '../../lib/handoffText';
import { hrefFor } from '../../lib/nav';
import { useCopy } from '../../hooks/useCopy';
import { AgentAvatar, AgentName, CopyCommand, Pill } from './ui';
import { BranchLabel, HandoffSections } from './Handoff';

const PAST_ROW = 56; // px; the baton travels exactly one row

export function LatestHandoff({
  latest,
  previous,
  arrivals,
  now,
}: {
  latest: TrailItem;
  previous?: TrailItem;
  /** Increments each time a new handoff arrives while the page is open. */
  arrivals: number;
  now: Date;
}) {
  const reduceMotion = useReducedMotion();
  const [copyPrompt] = useCopy();
  const [prevArrivals, setPrevArrivals] = useState(arrivals);
  const [fresh, setFresh] = useState(false);
  if (arrivals !== prevArrivals) {
    setPrevArrivals(arrivals);
    setFresh(true);
  }
  useEffect(() => {
    if (!fresh) return undefined;
    const timer = window.setTimeout(() => setFresh(false), 4000);
    return () => window.clearTimeout(timer);
  }, [fresh]);
  const travel = fresh && previous && !reduceMotion;
  const meta = agentMeta(latest.agent_id);
  const project = latest.project_id && latest.project_id !== 'default' ? latest.project_id : null;
  const verified = latest.detail?.structured ? latest.detail.agent_verified : false;

  return (
    <article
      aria-labelledby="latest-handoff-title"
      className={clsx(
        'rr-card relative min-w-0 overflow-hidden rounded-[3px] transition-shadow duration-700',
        fresh && 'shadow-[0_0_0_2px_var(--signal),var(--shadow)]',
      )}
    >
      <header className="flex items-center gap-3 bg-head px-4 py-2.5 font-mono text-[11px] text-head-ink sm:px-5">
        <span className="shrink-0 uppercase tracking-[0.08em]">
          <span className="text-signal" aria-hidden="true">
            ${' '}
          </span>
          <span id="latest-handoff-title">last handoff</span>
        </span>
        {project && <span className="min-w-0 truncate opacity-70">--project {project}</span>}
        <time
          dateTime={latest.created_at}
          title={absoluteTime(latest.created_at)}
          className="ml-auto shrink-0 opacity-80"
        >
          {relativeTime(latest.created_at, now)}
        </time>
      </header>

      <div className="px-4 pb-4 pt-4 sm:px-5">
        <div className="relative pl-10">
          {/* the rail, from the previous agent down to the current one */}
          {previous && (
            <>
              <span
                aria-hidden="true"
                className="rr-rail absolute left-[15px] top-[14px] w-[2px]"
                style={{ height: PAST_ROW }}
              />
              {travel && (
                <motion.span
                  key={`fill-${arrivals}`}
                  aria-hidden="true"
                  className="absolute left-[15px] top-[14px] w-[2px] origin-top bg-signal"
                  style={{ height: PAST_ROW }}
                  initial={{ scaleY: 0 }}
                  animate={{ scaleY: [0, 1, 1], opacity: [1, 1, 0] }}
                  transition={{ duration: 1.6, times: [0, 0.55, 1], ease: [0.6, 0, 0.2, 1] }}
                />
              )}
            </>
          )}
          <motion.span
            key={`baton-${arrivals}`}
            aria-hidden="true"
            className="rr-baton absolute left-[10px] z-[2] h-[26px] w-[12px]"
            style={{ top: (previous ? PAST_ROW : 0) + 1 }}
            initial={travel ? { y: -PAST_ROW, rotate: 0 } : false}
            animate={travel ? { y: 0, rotate: [0, -8, 0] } : { y: 0 }}
            transition={{ duration: 0.9, ease: [0.6, 0, 0.2, 1] }}
          />

          {previous && (
            <div className="relative flex items-center gap-2.5" style={{ height: PAST_ROW }}>
              <span
                aria-hidden="true"
                className="absolute -left-[31px] top-[8px] h-3 w-3 rounded-full border-2 border-ink-3 bg-panel"
              />
              <span className="-mt-5 flex min-w-0 items-center gap-2 text-sm text-ink-3">
                <AgentAvatar agentId={previous.agent_id} size="sm" />
                <span className="min-w-0 truncate">
                  <span className="font-semibold text-ink-2">{agentMeta(previous.agent_id).name}</span> handed off{' '}
                  {relativeTime(previous.created_at, now)}
                  {where(previous.branch, previous.head_commit) && (
                    <span className="font-mono text-[11px]"> · {where(previous.branch, previous.head_commit)}</span>
                  )}
                </span>
              </span>
            </div>
          )}

          <div className="relative">
            <span
              aria-hidden="true"
              className="absolute -left-[31px] top-[8px] h-3 w-3 rounded-full border-2 border-signal bg-signal"
            />
            <div className="flex min-w-0 items-start gap-3">
              <AgentAvatar agentId={latest.agent_id} size="lg" />
              <div className="min-w-0 flex-1">
                <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <AgentName agentId={latest.agent_id} className="text-xl" />
                  {fresh && <Pill tone="signal">new</Pill>}
                  {verified && (
                    <span className="inline-flex items-center gap-1 font-mono text-[11px] text-ok" title="Closed with an agent-scoped key">
                      <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" /> verified agent
                    </span>
                  )}
                </p>
                <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1">
                  <BranchLabel branch={latest.branch} sha={latest.head_commit} />
                  {latest.failing > 0 && <Pill tone="fail">{latest.failing} failing</Pill>}
                  {latest.open > 0 && <Pill tone="open">{latest.open} open</Pill>}
                </p>
                <p className="mt-2 text-[15px] leading-snug text-ink [overflow-wrap:anywhere]">{latest.headline}</p>
              </div>
            </div>
          </div>
        </div>

        {latest.detail && (
          <div className="mt-4 border border-rule border-l-[3px] border-l-signal bg-paper px-3 py-3 sm:px-4">
            <HandoffSections detail={latest.detail} max={3} />
          </div>
        )}

        <div className="mt-4">
          <p className="mb-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Continue with</p>
          <CopyCommand
            command={continueCommand(latest)}
            label="Command to continue from this handoff"
            toastText="Command copied. Paste it where the next agent starts."
          />
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => copyPrompt(continuePrompt(latest), 'Prompt copied. Paste it into any agent.')}
              className="rr-btn-ghost inline-flex items-center gap-1.5 px-3 py-2 text-sm"
            >
              <ClipboardCopy className="h-4 w-4" aria-hidden="true" /> Copy as a prompt
            </button>
            <a
              href={hrefFor('trail', { project: latest.project_id, open: latest.id })}
              className="inline-flex items-center gap-1 px-2 py-2 text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4 hover:text-signal-ink"
            >
              Full handoff on the trail <ArrowRight className="h-4 w-4" aria-hidden="true" />
            </a>
          </div>
        </div>
      </div>
      <p className="sr-only" aria-live="polite">
        {fresh ? `New handoff from ${meta.name}.` : ''}
      </p>
    </article>
  );
}
