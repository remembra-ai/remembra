import { motion } from 'framer-motion';
import type { ElementType } from 'react';
import {
  Activity,
  ArrowRight,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Database,
  GitBranch,
  KeyRound,
  Network,
  Plus,
  Radio,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
  Workflow,
  Zap,
} from 'lucide-react';
import clsx from 'clsx';

interface ControlPlaneOverviewProps {
  memoryCount: number;
  entityCount: number;
  storageUsed: string;
  apiCalls: number;
  loading?: boolean;
  wsConnected: boolean;
  currentProjectId: string;
  onNewMemory: () => void;
  onSearch: () => void;
  onOpenDebugger: () => void;
  onOpenGraph: () => void;
}

const pipeline = [
  { label: 'Capture', detail: 'Agents, apps, docs', icon: Radio },
  { label: 'Extract', detail: 'Facts and entities', icon: Sparkles },
  { label: 'Resolve', detail: 'People, orgs, aliases', icon: Network },
  { label: 'Rank', detail: 'Hybrid recall', icon: Zap },
  { label: 'Govern', detail: 'Scopes and audit', icon: ShieldCheck },
];

const connectors = [
  { name: 'Claude Code', status: 'MCP ready', tone: 'ready' },
  { name: 'Cursor', status: 'Shared memory', tone: 'ready' },
  { name: 'Codex', status: 'Context bridge', tone: 'ready' },
  { name: 'OpenAI Agents', status: 'Session memory', tone: 'next' },
  { name: 'Slack / Discord', status: 'Inbox ready', tone: 'next' },
  { name: 'GitHub / Linear', status: 'Roadmap', tone: 'planned' },
];

function formatCount(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return value.toLocaleString();
}

function MetricCard({
  label,
  value,
  detail,
  icon: Icon,
  accent,
  loading,
}: {
  label: string;
  value: string;
  detail: string;
  icon: ElementType;
  accent: string;
  loading?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-[hsl(var(--border))/0.72] bg-[hsl(var(--card))/0.72] p-4 shadow-[inset_0_1px_0_hsl(0_0%_100%/0.04)]">
      <div className="mb-4 flex items-center justify-between">
        <div className={clsx('rounded-xl border p-2.5', accent)}>
          <Icon className="h-4 w-4" />
        </div>
        <span className="text-[10px] font-semibold uppercase tracking-[0.24em] text-[hsl(var(--muted-foreground))]">
          Live
        </span>
      </div>
      <div className="text-2xl font-semibold tracking-tight text-[hsl(var(--foreground))]">
        {loading ? '...' : value}
      </div>
      <div className="mt-1 text-xs font-medium text-[hsl(var(--muted-foreground))]">{label}</div>
      <p className="mt-3 text-xs leading-5 text-[hsl(var(--muted-foreground))]">{detail}</p>
    </div>
  );
}

export function ControlPlaneOverview({
  memoryCount,
  entityCount,
  storageUsed,
  apiCalls,
  loading = false,
  wsConnected,
  currentProjectId,
  onNewMemory,
  onSearch,
  onOpenDebugger,
  onOpenGraph,
}: ControlPlaneOverviewProps) {
  return (
    <motion.section
      initial={{ opacity: 0, y: 18 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.32, ease: [0.22, 1, 0.36, 1] }}
      className="mb-7 overflow-hidden rounded-[4px] border border-[hsl(var(--border))/0.72] bg-[linear-gradient(135deg,hsl(var(--card))/0.92,hsl(var(--background))/0.76)] shadow-[0_28px_70px_hsl(0_0%_0%/0.18),inset_0_1px_0_hsl(0_0%_100%/0.06)]"
    >
      <div className="grid gap-0 lg:grid-cols-[0.9fr_1.1fr]">
        <div className="border-b border-[hsl(var(--border))/0.72] p-5 md:p-7 lg:border-b-0 lg:border-r">
          <div className="mb-5 flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-2 rounded-full border border-cyan-400/25 bg-cyan-400/10 px-3 py-1.5 text-xs font-semibold uppercase tracking-[0.18em] text-cyan-300">
              <BrainCircuit className="h-3.5 w-3.5" />
              Memory Control Plane
            </span>
            <span
              className={clsx(
                'inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium',
                wsConnected
                  ? 'border-emerald-400/25 bg-emerald-400/10 text-emerald-300'
                  : 'border-amber-400/25 bg-amber-400/10 text-amber-300',
              )}
            >
              <span className={clsx('h-1.5 w-1.5 rounded-full', wsConnected ? 'bg-emerald-300 sync-pulse' : 'bg-amber-300')} />
              {wsConnected ? 'Live sync' : 'Reconnecting'}
            </span>
          </div>

          <h2 className="max-w-2xl text-3xl font-semibold leading-tight tracking-tight text-[hsl(var(--foreground))] md:text-4xl">
            Operate the shared brain behind every agent.
          </h2>
          <p className="mt-4 max-w-2xl text-sm leading-6 text-[hsl(var(--muted-foreground))] md:text-base">
            Project <span className="font-medium text-[hsl(var(--foreground))]">{currentProjectId}</span> is the memory bank for code agents, assistants, automations, and human workflows. Store context once, inspect why it returns, and route only the right truth to the right tool.
          </p>

          <div className="mt-6 grid gap-3 sm:grid-cols-3">
            <button
              onClick={onSearch}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-[hsl(var(--border))/0.76] bg-[hsl(var(--card))/0.72] px-4 text-sm font-medium text-[hsl(var(--foreground))] transition hover:bg-[hsl(var(--muted))/0.72]"
            >
              <Search className="h-4 w-4 text-cyan-300" />
              Search
            </button>
            <button
              onClick={onNewMemory}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl bg-[linear-gradient(135deg,hsl(var(--primary)),hsl(var(--shell-glow)))] px-4 text-sm font-medium text-white shadow-[0_16px_34px_hsl(var(--primary)/0.22)] transition hover:brightness-110"
            >
              <Plus className="h-4 w-4" />
              New Memory
            </button>
            <button
              onClick={onOpenDebugger}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl border border-amber-300/25 bg-amber-300/10 px-4 text-sm font-medium text-amber-300 transition hover:bg-amber-300/15"
            >
              <Activity className="h-4 w-4" />
              Debug
            </button>
          </div>

          <div className="mt-6 grid gap-3 sm:grid-cols-2">
            <MetricCard
              label="Stored memories"
              value={formatCount(memoryCount)}
              detail="Facts, notes, decisions, and agent handoffs available for recall."
              icon={Database}
              accent="border-violet-300/25 bg-violet-300/10 text-violet-200"
              loading={loading}
            />
            <MetricCard
              label="Resolved entities"
              value={formatCount(entityCount)}
              detail="People, organizations, projects, and concepts joined into a graph."
              icon={Users}
              accent="border-emerald-300/25 bg-emerald-300/10 text-emerald-300"
              loading={loading}
            />
            <MetricCard
              label="Recall traffic today"
              value={formatCount(apiCalls)}
              detail="Store and recall activity moving through this workspace."
              icon={Workflow}
              accent="border-cyan-300/25 bg-cyan-300/10 text-cyan-300"
              loading={loading}
            />
            <MetricCard
              label="Estimated storage"
              value={storageUsed}
              detail="Lightweight context now, compounding leverage over time."
              icon={KeyRound}
              accent="border-rose-300/25 bg-rose-300/10 text-rose-200"
              loading={loading}
            />
          </div>
        </div>

        <div className="p-5 md:p-7">
          <div className="mb-5 flex items-center justify-between gap-4">
            <div>
              <h3 className="text-base font-semibold text-[hsl(var(--foreground))]">Recall pipeline</h3>
              <p className="mt-1 text-xs leading-5 text-[hsl(var(--muted-foreground))]">
                The path from raw context to model-ready memory.
              </p>
            </div>
            <button
              onClick={onOpenGraph}
              className="inline-flex items-center gap-2 rounded-xl border border-cyan-300/25 bg-cyan-300/10 px-3 py-2 text-xs font-medium text-cyan-300 transition hover:bg-cyan-300/15"
            >
              Graph
              <ArrowRight className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="grid gap-3">
            {pipeline.map((step, index) => {
              const Icon = step.icon;
              return (
                <div
                  key={step.label}
                  className="grid grid-cols-[40px_1fr_auto] items-center gap-3 rounded-2xl border border-[hsl(var(--border))/0.62] bg-[hsl(var(--card))/0.54] p-3"
                >
                  <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-[hsl(var(--border))/0.72] bg-[hsl(var(--background))/0.6] text-[hsl(var(--foreground))]">
                    <Icon className="h-4 w-4" />
                  </div>
                  <div className="min-w-0">
                    <div className="text-sm font-semibold text-[hsl(var(--foreground))]">{step.label}</div>
                    <div className="text-xs text-[hsl(var(--muted-foreground))]">{step.detail}</div>
                  </div>
                  <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[hsl(var(--muted-foreground))]">
                    0{index + 1}
                  </span>
                </div>
              );
            })}
          </div>

          <div className="mt-5 grid gap-4 xl:grid-cols-[1fr_0.9fr]">
            <div className="rounded-2xl border border-[hsl(var(--border))/0.62] bg-[hsl(var(--card))/0.52] p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-[hsl(var(--foreground))]">
                <GitBranch className="h-4 w-4 text-emerald-300" />
                Operator watchlist
              </div>
              <div className="space-y-2">
                {[
                  ['Stale context', 'Decay report monitors what should fade.'],
                  ['Conflicting facts', 'Debugger exposes what needs review.'],
                  ['Team scopes', 'Shared memory stays inside boundaries.'],
                  ['Agent access', 'API keys and audit trails stay visible.'],
                ].map(([label, detail]) => (
                  <div key={label} className="flex gap-3 rounded-xl bg-[hsl(var(--background))/0.48] p-3">
                    <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-300" />
                    <div>
                      <div className="text-xs font-semibold text-[hsl(var(--foreground))]">{label}</div>
                      <div className="mt-0.5 text-xs leading-5 text-[hsl(var(--muted-foreground))]">{detail}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-2xl border border-[hsl(var(--border))/0.62] bg-[hsl(var(--card))/0.52] p-4">
              <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-[hsl(var(--foreground))]">
                <Clock3 className="h-4 w-4 text-amber-300" />
                Connector matrix
              </div>
              <div className="grid gap-2">
                {connectors.map((connector) => (
                  <div key={connector.name} className="flex items-center justify-between gap-3 rounded-xl bg-[hsl(var(--background))/0.48] px-3 py-2.5">
                    <span className="truncate text-xs font-medium text-[hsl(var(--foreground))]">{connector.name}</span>
                    <span
                      className={clsx(
                        'rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.12em]',
                        connector.tone === 'ready' && 'bg-emerald-300/10 text-emerald-300',
                        connector.tone === 'next' && 'bg-cyan-300/10 text-cyan-300',
                        connector.tone === 'planned' && 'bg-[hsl(var(--muted))/0.72] text-[hsl(var(--muted-foreground))]',
                      )}
                    >
                      {connector.status}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </motion.section>
  );
}
