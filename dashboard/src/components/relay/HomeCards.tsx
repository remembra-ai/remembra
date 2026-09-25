// Home page cards: connect checklist, weekly recap and the first-handoff
// celebration. The plan meter lives in ../credits/Credits.

import { useId } from 'react';
import clsx from 'clsx';
import { motion, useReducedMotion } from 'framer-motion';
import { ArrowRight, Check, Copy, X } from 'lucide-react';
import type { ActivitySummary, AgentActivity, TrailItem } from '../../lib/relay';
import { api } from '../../lib/api';
import { CONNECTABLE_AGENTS, PIPX_INSTALL, agentMeta, canonicalAgentId, saveKeyCommand } from '../../lib/agents';
import { hrefFor } from '../../lib/nav';
import { relativeTime } from '../../lib/time';
import { useCopy } from '../../hooks/useCopy';
import { Card, CardHeader, CopyCommand, Sparkline } from './ui';

function agentConnectCommand(agentId: string): string {
  const meta = agentMeta(agentId);
  if (meta.verified) return `remembra-relay connect --apply --agent ${meta.adapter}`;
  return `remembra-relay connect --apply --agent ${meta.adapter} --include-unverified`;
}

/**
 * Setup checklist: a key, the install, connect, then one row per agent that
 * ticks itself off when that agent's first handoff arrives. Without a key the
 * hooks run but cannot reach the server, so the key comes first.
 */
export function ConnectChecklist({
  agents,
  now,
  onDismiss,
}: {
  agents: AgentActivity[];
  now: Date;
  onDismiss?: () => void;
}) {
  const titleId = useId();
  const [copy] = useCopy();
  const seen = new Map<string, AgentActivity>();
  for (const agent of agents) seen.set(canonicalAgentId(agent.agent_id), agent);
  const connected = CONNECTABLE_AGENTS.filter((id) => seen.has(id)).length;
  const others = agents.filter((a) => !CONNECTABLE_AGENTS.includes(canonicalAgentId(a.agent_id)) && a.agent_id !== 'dashboard');
  const unverifiedNames = CONNECTABLE_AGENTS.filter((id) => !agentMeta(id).verified).map((id) => agentMeta(id).name);
  const verifiedNames = CONNECTABLE_AGENTS.filter((id) => agentMeta(id).verified).map((id) => agentMeta(id).name);

  return (
    <Card labelledBy={titleId}>
      <CardHeader
        id={titleId}
        eyebrow={`Setup · ${connected} of ${CONNECTABLE_AGENTS.length} agents`}
        title="Connect your agents"
        action={
          onDismiss && (
            <button
              type="button"
              onClick={onDismiss}
              className="rounded-[2px] p-1.5 text-ink-3 hover:bg-paper-2 hover:text-ink"
              aria-label="Hide the setup checklist"
            >
              <X className="h-4 w-4" />
            </button>
          )
        }
      />
      <div className="px-4 pb-4 pt-3 sm:px-5">
        <ol className="space-y-4">
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">1. Create an API key.</span> The relay signs in to this server with it; without one
              nothing reaches your trail.{' '}
              <a href={hrefFor('keys')} className="font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4">
                Open API keys
              </a>
            </p>
          </li>
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">2. Install, then save the key.</span> Replace{' '}
              <code className="font-mono text-[13px]">&lt;your-key&gt;</code>. This stores it in{' '}
              <code className="font-mono text-[13px]">~/.remembra/credentials</code>, where the relay reads it, and adds the Remembra MCP
              server to the agents it finds. Skip it if your agents already have <code className="font-mono text-[13px]">REMEMBRA_API_KEY</code>.
            </p>
            <CopyCommand className="mt-2" command={PIPX_INSTALL} label="Install command" toastText="Install command copied" />
            <CopyCommand
              className="mt-2"
              command={saveKeyCommand(api.getApiBaseUrl())}
              label="Save-key command"
              toastText="Command copied: replace <your-key> before running it"
            />
          </li>
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">3. Connect.</span> <code className="font-mono text-[13px]">remembra-relay connect</code>{' '}
              is a dry run that shows every change; nothing is written until you add <code className="font-mono text-[13px]">--apply</code>.
            </p>
            <CopyCommand className="mt-2" command="remembra-relay connect --apply" label="Apply command" toastText="Apply command copied" />
            <p className="mt-1.5 text-xs text-ink-3">
              This writes hooks for {verifiedNames.join(', ')} only, the verified adapter today. For {unverifiedNames.join(', ')}, copy that
              agent’s command below: it adds <code className="font-mono">--include-unverified</code>.
            </p>
          </li>
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">4. Start a session and end it.</span> Each agent ticks off here when its first handoff
              arrives.
            </p>
            <div className="mt-2 flex gap-1" aria-hidden="true">
              {CONNECTABLE_AGENTS.map((id) => (
                <span key={id} className={clsx('h-1 flex-1 rounded-full', seen.has(id) ? 'bg-signal' : 'rr-rail-h')} />
              ))}
            </div>
            <ul className="mt-2 divide-y divide-rule border-y border-rule">
              {CONNECTABLE_AGENTS.map((id) => {
                const activity = seen.get(id);
                const meta = agentMeta(id);
                return (
                  <li key={id} className="flex items-center gap-3 py-2">
                    <span
                      className={clsx(
                        'flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2',
                        activity ? 'border-ok bg-ok text-panel' : 'border-rule',
                      )}
                      aria-hidden="true"
                    >
                      {activity && <Check className="h-3 w-3" strokeWidth={3} />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold text-ink">{meta.name}</span>
                      <span className="block truncate font-mono text-[11px] text-ink-3">
                        {activity
                          ? `connected · last handoff ${relativeTime(activity.last_active, now)}`
                          : meta.verified
                            ? 'waiting for its first handoff'
                            : 'waiting · adapter not yet verified'}
                      </span>
                    </span>
                    <span className="sr-only">{activity ? 'Connected' : 'Not connected yet'}</span>
                    {!activity && (
                      <button
                        type="button"
                        onClick={() => copy(agentConnectCommand(id), `Command for ${meta.name} copied`)}
                        className="rr-btn-ghost inline-flex shrink-0 items-center gap-1 px-2 py-1 font-mono text-[11px]"
                        aria-label={`Copy the connect command for ${meta.name}`}
                      >
                        <Copy className="h-3 w-3" aria-hidden="true" /> command
                      </button>
                    )}
                  </li>
                );
              })}
              {others.map((agent) => (
                <li key={agent.agent_id} className="flex items-center gap-3 py-2">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 border-ok bg-ok text-panel" aria-hidden="true">
                    <Check className="h-3 w-3" strokeWidth={3} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold text-ink">{agentMeta(agent.agent_id).name}</span>
                    <span className="block truncate font-mono text-[11px] text-ink-3">
                      via MCP · last handoff {relativeTime(agent.last_active, now)}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </li>
        </ol>
        <p className="mt-3 text-xs text-ink-3">
          Any MCP agent works too: the Remembra MCP server tells it to call <code className="font-mono">session_brief</code> at start and{' '}
          <code className="font-mono">close_session</code> before it stops. Your server URL is{' '}
          <code className="font-mono [overflow-wrap:anywhere]">{api.getApiBaseUrl()}</code>; Connection in the sidebar (under More on a
          phone) has the rest of the MCP config.
        </p>
      </div>
    </Card>
  );
}

function plural(n: number, word: string, many = `${word}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? word : many}`;
}

/** Sum of every project's daily series: all activity per day. */
function dailyTotals(summary: ActivitySummary): number[] {
  const totals = new Array<number>(summary.days).fill(0);
  for (const project of summary.projects) {
    project.daily.forEach((value, index) => {
      totals[index] += value;
    });
  }
  return totals;
}

export function WeeklyRecap({ summary, now }: { summary: ActivitySummary; now: Date }) {
  const titleId = useId();
  const week = summary.week;
  const last7 = dailyTotals(summary).slice(-7);
  const top = [...summary.agents].sort((a, b) => b.sessions_7d - a.sessions_7d)[0];
  const busiestIndex = last7.indexOf(Math.max(...last7));
  const busiestDay = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (6 - busiestIndex));
  const quiet = week.handoffs === 0 && week.checkpoints === 0;

  return (
    <Card labelledBy={titleId}>
      <CardHeader id={titleId} eyebrow="Last 7 days" title="This week" />
      <div className="px-4 pb-4 pt-2 sm:px-5">
        {quiet ? (
          <p className="text-sm text-ink-2">
            A quiet week: no handoffs yet. Recaps fill in as your agents close sessions.
          </p>
        ) : (
          <>
            <p className="font-display text-[22px] font-bold leading-tight tracking-tight text-ink">
              {plural(week.handoffs, 'handoff')} across {plural(week.agents.length, 'agent')},{' '}
              {plural(week.projects.length, 'project')}.
            </p>
            <Sparkline
              className="mt-3"
              values={last7}
              height={36}
              label={`Activity per day for the last 7 days: ${last7.join(', ')}`}
            />
            <ul className="mt-3 space-y-1 text-sm text-ink-2">
              {top && top.sessions_7d > 0 && (
                <li>
                  Most active: <span className="font-semibold text-ink">{agentMeta(top.agent_id).name}</span> with{' '}
                  {plural(top.sessions_7d, 'session')}
                </li>
              )}
              {Math.max(...last7) > 0 && (
                <li>
                  Busiest day: {busiestIndex === 6 ? 'today' : busiestDay.toLocaleDateString(undefined, { weekday: 'long' })} (
                  {plural(last7[busiestIndex], 'entry', 'entries')})
                </li>
              )}
              {week.checkpoints > 0 && <li>{plural(week.checkpoints, 'checkpoint')} saved mid-session</li>}
            </ul>
          </>
        )}
      </div>
    </Card>
  );
}

/** Shown once, the first time a new user's first handoff is on the trail. */
export function FirstHandoffCelebration({ item, onDismiss }: { item: TrailItem; onDismiss: () => void }) {
  const reduceMotion = useReducedMotion();
  const meta = agentMeta(item.agent_id);
  const project = item.project_id && item.project_id !== 'default' ? item.project_id : 'this project';
  return (
    <section
      aria-labelledby="first-handoff-title"
      className="relative overflow-hidden rounded-[3px] border border-rule-strong bg-head text-head-ink"
    >
      <div className="relative h-10" aria-hidden="true">
        <span className="rr-rail-h absolute left-4 right-4 top-1/2 h-[2px] -translate-y-1/2 opacity-60" />
        <motion.span
          className="rr-baton absolute top-1/2 h-[12px] w-[30px] -translate-y-1/2"
          initial={reduceMotion ? false : { left: '2%', opacity: 0 }}
          animate={{ left: 'calc(100% - 48px)', opacity: 1 }}
          transition={{ duration: 1.4, ease: [0.6, 0, 0.2, 1] }}
        />
      </div>
      <div className="flex flex-col gap-3 px-4 pb-4 sm:flex-row sm:items-end sm:justify-between sm:px-5">
        <div className="min-w-0">
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-signal">First handoff received</p>
          <h2 id="first-handoff-title" className="font-display mt-1 text-2xl font-extrabold leading-tight tracking-tight">
            {meta.name} left its first trail.
          </h2>
          <p className="mt-1 max-w-xl text-sm opacity-80">
            The next agent that starts in {project}, in any tool, on any machine, picks up right here.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={hrefFor('trail', { open: item.id, project: item.project_id })}
            className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-2 text-sm"
          >
            See it on the trail <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </a>
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-[3px] border border-white/20 px-3 py-2 text-sm hover:border-white/60"
          >
            Nice
          </button>
        </div>
      </div>
      <span className="sr-only" aria-live="polite">
        First handoff received from {meta.name}.
      </span>
    </section>
  );
}
