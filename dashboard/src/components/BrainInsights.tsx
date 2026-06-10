import { useState, useEffect, useCallback } from 'react';
import { Brain, RefreshCw, Sparkles, Network, Zap, AlertTriangle } from 'lucide-react';
import clsx from 'clsx';
import {
  api,
  type BrainInsightsResponse,
  type BrainCommunity,
  type BrainGodNode,
  type BrainSurprisingLink,
} from '../lib/api';
import { communityColor } from '../lib/communityColors';

interface BrainInsightsProps {
  projectId?: string;
}

/**
 * The "brain" panel — Remembra's higher-level understanding of a memory graph.
 * Surfaces auto-discovered themes (communities) with summaries, the most central
 * entities, and surprising cross-theme links. Mirrors the GraphRAG community
 * model, computed by the backend's pure-Python Louvain engine.
 */
export function BrainInsights({ projectId }: BrainInsightsProps) {
  const [data, setData] = useState<BrainInsightsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getBrainInsights(projectId || undefined));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load brain insights');
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  const reanalyze = useCallback(async () => {
    setAnalyzing(true);
    setError(null);
    try {
      // Persisted recompute — also writes community ids so the graph recolors.
      setData(await api.analyzeBrain(projectId || undefined));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Analysis failed');
    } finally {
      setAnalyzing(false);
    }
  }, [projectId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400">
        <RefreshCw className="w-5 h-5 animate-spin mr-2" />
        Mapping your memory…
      </div>
    );
  }

  const coherence = data ? Math.round(Math.max(0, Math.min(1, data.modularity)) * 100) : 0;
  const isEmpty = !data || data.num_entities === 0;

  return (
    <div className="space-y-6">
      {/* Header + stats */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="p-2.5 rounded-xl bg-purple-500/15 border border-purple-500/25">
            <Brain className="w-5 h-5 text-purple-300" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-white">Brain</h2>
            <p className="text-xs text-gray-400">Themes, central memories, and surprising links</p>
          </div>
        </div>
        <button
          onClick={reanalyze}
          disabled={analyzing}
          className={clsx(
            'flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium transition-colors',
            'bg-purple-500/80 hover:bg-purple-500 text-white shadow-[0_0_18px_rgba(168,85,247,0.35)]',
            analyzing && 'opacity-60 cursor-not-allowed'
          )}
        >
          <Sparkles className={clsx('w-4 h-4', analyzing && 'animate-pulse')} />
          {analyzing ? 'Analyzing…' : 'Analyze'}
        </button>
      </div>

      {error && (
        <div className="flex items-center gap-2 px-4 py-3 rounded-xl bg-red-500/10 border border-red-500/25 text-sm text-red-300">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          {error}
        </div>
      )}

      {!error && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard label="Entities" value={data?.num_entities ?? 0} />
          <StatCard label="Links" value={data?.num_relationships ?? 0} />
          <StatCard label="Themes" value={data?.num_communities ?? 0} accent />
          <StatCard label="Coherence" value={`${coherence}%`} hint="How cleanly themes separate" />
        </div>
      )}

      {isEmpty && !error ? (
        <EmptyState onAnalyze={reanalyze} analyzing={analyzing} />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ThemesPanel communities={data?.communities ?? []} />
          <div className="space-y-6">
            <GodNodesPanel nodes={data?.god_nodes ?? []} />
            <SurprisingLinksPanel links={data?.surprising_links ?? []} />
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  accent,
  hint,
}: {
  label: string;
  value: number | string;
  accent?: boolean;
  hint?: string;
}) {
  return (
    <div
      className={clsx(
        'rounded-2xl border px-4 py-3',
        accent ? 'bg-purple-500/[0.08] border-purple-500/25' : 'bg-white/[0.03] border-white/10'
      )}
      title={hint}
    >
      <p className="text-[11px] uppercase tracking-wider text-gray-400">{label}</p>
      <p className={clsx('text-2xl font-semibold', accent ? 'text-purple-200' : 'text-white')}>{value}</p>
    </div>
  );
}

function ThemesPanel({ communities }: { communities: BrainCommunity[] }) {
  return (
    <section className="rounded-2xl border border-white/10 bg-white/[0.02] p-5">
      <div className="flex items-center gap-2 mb-4">
        <Network className="w-4 h-4 text-gray-300" />
        <h3 className="text-sm font-semibold text-white">Themes</h3>
        <span className="text-xs text-gray-500">{communities.length}</span>
      </div>
      {communities.length === 0 ? (
        <p className="text-sm text-gray-500">No themes yet — add more connected memories.</p>
      ) : (
        <div className="space-y-3">
          {communities.map((c) => (
            <div
              key={c.community_index}
              className="rounded-xl border border-white/5 bg-white/[0.02] hover:bg-white/[0.04] transition-colors p-4"
            >
              <div className="flex items-center gap-2.5 mb-1.5">
                <span
                  className="w-3 h-3 rounded-full shrink-0"
                  style={{
                    backgroundColor: communityColor(c.community_index),
                    boxShadow: `0 0 10px ${communityColor(c.community_index)}`,
                  }}
                />
                <h4 className="text-sm font-medium text-white truncate">{c.label}</h4>
                <span className="ml-auto text-[11px] text-gray-500 shrink-0">{c.size} entities</span>
              </div>
              {c.summary && <p className="text-xs text-gray-400 leading-relaxed mb-2.5">{c.summary}</p>}
              <div className="flex flex-wrap gap-1.5">
                {c.top_entities.slice(0, 6).map((e) => (
                  <span
                    key={e.id}
                    className="text-[11px] px-2 py-0.5 rounded-md bg-white/[0.05] text-gray-300 border border-white/5"
                  >
                    {e.name}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function GodNodesPanel({ nodes }: { nodes: BrainGodNode[] }) {
  const max = nodes.length ? Math.max(...nodes.map((n) => n.centrality)) || 1 : 1;
  return (
    <section className="rounded-2xl border border-white/10 bg-white/[0.02] p-5">
      <div className="flex items-center gap-2 mb-4">
        <Zap className="w-4 h-4 text-amber-300" />
        <h3 className="text-sm font-semibold text-white">Central memories</h3>
      </div>
      {nodes.length === 0 ? (
        <p className="text-sm text-gray-500">No central entities yet.</p>
      ) : (
        <div className="space-y-2.5">
          {nodes.slice(0, 8).map((n) => (
            <div key={n.id} className="flex items-center gap-3">
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: communityColor(n.community_index) }}
              />
              <span className="text-sm text-gray-200 truncate w-32 shrink-0">{n.name}</span>
              <div className="flex-1 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${Math.max(6, (n.centrality / max) * 100)}%`,
                    backgroundColor: communityColor(n.community_index),
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SurprisingLinksPanel({ links }: { links: BrainSurprisingLink[] }) {
  return (
    <section className="rounded-2xl border border-white/10 bg-white/[0.02] p-5">
      <div className="flex items-center gap-2 mb-4">
        <Sparkles className="w-4 h-4 text-cyan-300" />
        <h3 className="text-sm font-semibold text-white">Surprising links</h3>
      </div>
      {links.length === 0 ? (
        <p className="text-sm text-gray-500">No cross-theme connections found.</p>
      ) : (
        <div className="space-y-2">
          {links.slice(0, 8).map((l, i) => (
            <div key={i} className="flex items-center gap-2 text-sm">
              <span className="px-2 py-0.5 rounded-md bg-white/[0.05] text-gray-200 border border-white/5 truncate max-w-[40%]">
                {l.from}
              </span>
              <span className="text-gray-500 shrink-0">↔</span>
              <span className="px-2 py-0.5 rounded-md bg-white/[0.05] text-gray-200 border border-white/5 truncate max-w-[40%]">
                {l.to}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EmptyState({ onAnalyze, analyzing }: { onAnalyze: () => void; analyzing: boolean }) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-16 rounded-2xl border border-dashed border-white/10 bg-white/[0.02]">
      <div className="p-4 rounded-2xl bg-purple-500/10 border border-purple-500/20 mb-4">
        <Brain className="w-8 h-8 text-purple-300" />
      </div>
      <h3 className="text-base font-semibold text-white mb-1">No themes mapped yet</h3>
      <p className="text-sm text-gray-400 max-w-sm mb-5">
        Once you have connected memories, Remembra clusters them into themes and surfaces the
        ideas at the center of your knowledge.
      </p>
      <button
        onClick={onAnalyze}
        disabled={analyzing}
        className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium bg-purple-500/80 hover:bg-purple-500 text-white transition-colors disabled:opacity-60"
      >
        <Sparkles className={clsx('w-4 h-4', analyzing && 'animate-pulse')} />
        {analyzing ? 'Analyzing…' : 'Analyze now'}
      </button>
    </div>
  );
}
