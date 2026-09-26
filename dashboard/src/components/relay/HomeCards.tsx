// Home page cards: connect checklist, weekly recap and the first-handoff
// celebration. The plan meter lives in ../credits/Credits.

import { useId, useState } from 'react';
import clsx from 'clsx';
import { PixelHandoff } from '../../brand/PixelHandoff';
import { ArrowRight, Check, Copy, KeyRound, Loader2, X } from 'lucide-react';
import type { ActivitySummary, AgentActivity, TrailItem } from '../../lib/relay';
import { api } from '../../lib/api';
import { CONNECTABLE_AGENTS, agentMeta, canonicalAgentId, oneLineInstall } from '../../lib/agents';
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
 * Create an editor key for the relay right here. It is shown once, with its
 * own copy button: the install command asks for it at a hidden prompt, so it
 * never lands in shell history.
 */
function RelayKeyStep({ newKey, onKey }: { newKey: string | null; onKey: (key: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copy, copied] = useCopy();
  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      const host = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform) ? 'mac' : 'machine';
      const created = await api.createKey(`relay (${host}, ${new Date().toISOString().slice(0, 10)})`, 'editor');
      onKey(created.key);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The key could not be created.');
    } finally {
      setBusy(false);
    }
  };
  if (newKey) {
    return (
      <div className="mt-2 border-l-[3px] border-ok bg-ok-wash px-3 py-2 text-sm text-ink">
        <p className="flex items-start gap-2">
          <Check className="mt-0.5 h-4 w-4 shrink-0 text-ok" aria-hidden="true" />
          <span>
            Key created. It is shown only on this page: copy it now and paste it when the command below asks for it.
          </span>
        </p>
        <div role="group" aria-label="Your new relay key" className="mt-2 flex items-stretch rounded-[3px] border border-rule bg-panel">
          <code className="min-w-0 flex-1 overflow-x-auto px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">{newKey}</code>
          <button
            type="button"
            onClick={() => copy(newKey, 'Key copied')}
            aria-label="Copy the new relay key"
            className="flex shrink-0 items-center gap-1.5 border-l border-rule px-3 font-mono text-xs text-ink-2 hover:bg-paper-2"
          >
            {copied ? <Check className="h-3.5 w-3.5 text-ok" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
            <span>{copied ? 'Copied' : 'Copy key'}</span>
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <button type="button" onClick={create} disabled={busy} className="rr-btn-primary inline-flex items-center gap-1.5 px-3 py-2 text-sm">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> : <KeyRound className="h-4 w-4" aria-hidden="true" />}
        Create a relay key
      </button>
      <a href={hrefFor('keys')} className="text-sm font-semibold text-ink underline decoration-signal decoration-2 underline-offset-4">
        or use one from API keys
      </a>
      {error && (
        <p role="alert" className="basis-full text-sm text-fail">
          {error}
        </p>
      )}
    </div>
  );
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
  const [newKey, setNewKey] = useState<string | null>(null);
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
              <span className="font-semibold text-ink">1. A key for this machine.</span> The relay signs in with it; without one nothing
              reaches your trail.
            </p>
            <RelayKeyStep onKey={setNewKey} newKey={newKey} />
          </li>
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">2. One line in your terminal.</span> Installs Remembra, asks for the key at a
              hidden prompt (it never goes on the command line or into your shell history), shows what it will change in each agent and
              writes after you say yes: the key in <code className="font-mono text-[13px]">~/.remembra/credentials</code>, the MCP server
              in the agents it finds (owner-only files, a backup of each), then the relay hooks.
            </p>
            <CopyCommand
              className="mt-2"
              command={oneLineInstall(api.getApiBaseUrl())}
              label="One-line install and connect"
              toastText="Command copied: it asks for your key when you run it"
            />
            <p className="mt-1.5 text-xs text-ink-3">
              Want to see every change first? Run <code className="font-mono">remembra-relay connect</code> without{' '}
              <code className="font-mono">--apply</code>: it is a dry run. It writes hooks for {verifiedNames.join(', ')}, the verified
              adapter today; for {unverifiedNames.join(', ')}, copy that agent’s command below (it adds{' '}
              <code className="font-mono">--include-unverified</code>).
            </p>
          </li>
          <li>
            <p className="text-sm text-ink-2">
              <span className="font-semibold text-ink">3. Start a session and end it.</span> Each agent ticks off here when its first handoff
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
  const meta = agentMeta(item.agent_id);
  const project = item.project_id && item.project_id !== 'default' ? item.project_id : 'this project';
  return (
    <section
      aria-labelledby="first-handoff-title"
      className="relative overflow-hidden rounded-[3px] border border-rule-strong bg-head text-head-ink"
    >
      <PixelHandoff agentColor={meta.lane} className="block h-[60px] w-full text-head-ink" />
      <div className="flex flex-col gap-3 px-4 pb-4 sm:flex-row sm:items-end sm:justify-between sm:px-5">
        <div className="min-w-0">
          <p className="font-mono text-[11px] uppercase tracking-[0.08em] text-signal">handoff.saved · your first one</p>
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
