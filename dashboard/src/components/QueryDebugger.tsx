import { useState } from 'react';
import { api } from '../lib/api';
import type { DebugRecallResponse, ScoringBreakdown } from '../lib/api';
import { Search, Loader2, Zap, Clock, Users, Type, ChevronDown, ChevronRight } from 'lucide-react';
import clsx from 'clsx';

export function QueryDebugger() {
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<DebugRecallResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const runDebug = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.debugRecall(query, 10, 0.2);
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Debug recall failed');
    } finally {
      setLoading(false);
    }
  };

  const scoreColor = (score: number) => {
    if (score >= 0.8) return 'text-green-600 dark:text-green-400';
    if (score >= 0.6) return 'text-yellow-600 dark:text-yellow-400';
    if (score >= 0.4) return 'text-orange-600 dark:text-orange-400';
    return 'text-red-600 dark:text-red-400';
  };

  const barWidth = (score: number) => `${Math.max(2, Math.round(score * 100))}%`;

  const barColor = (score: number) => {
    if (score >= 0.8) return 'bg-green-500';
    if (score >= 0.6) return 'bg-yellow-500';
    if (score >= 0.4) return 'bg-orange-500';
    return 'bg-red-500';
  };

  return (
    <div className="space-y-6">
      {/* Query Input */}
      <div className="flex gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && runDebug()}
            placeholder="Enter a query to debug recall pipeline..."
            className="w-full pl-10 pr-4 py-2.5 rounded-lg border border-gray-300 dark:border-gray-600
                       bg-white dark:bg-gray-800 text-gray-900 dark:text-white
                       focus:ring-2 focus:ring-[#8B5CF6] focus:border-transparent"
          />
        </div>
        <button
          onClick={runDebug}
          disabled={loading || !query.trim()}
          className={clsx(
            'px-5 py-2.5 rounded-lg font-medium text-white',
            'bg-[#8B5CF6] hover:bg-[#7C3AED] disabled:opacity-50 disabled:cursor-not-allowed',
            'flex items-center gap-2'
          )}
        >
          {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Zap className="w-4 h-4" />}
          Debug
        </button>
      </div>

      {error && (
        <div className="p-3 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 text-sm">
          {error}
        </div>
      )}

      {result && (
        <>
          {/* Pipeline Summary */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="p-3 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-100 dark:border-blue-800">
              <div className="text-xs text-[#8B5CF6] dark:text-[#A78BFA] font-medium">Latency</div>
              <div className="text-lg font-bold text-blue-800 dark:text-blue-300">{result.latency_ms.toFixed(0)}ms</div>
            </div>
            <div className="p-3 rounded-lg bg-purple-50 dark:bg-purple-900/20 border border-purple-100 dark:border-purple-800">
              <div className="text-xs text-purple-600 dark:text-purple-400 font-medium">Candidates</div>
              <div className="text-lg font-bold text-purple-800 dark:text-purple-300">{result.total_candidates}</div>
            </div>
            <div className="p-3 rounded-lg bg-green-50 dark:bg-green-900/20 border border-green-100 dark:border-green-800">
              <div className="text-xs text-green-600 dark:text-green-400 font-medium">Entities Matched</div>
              <div className="text-lg font-bold text-green-800 dark:text-green-300">{result.matched_entities.length}</div>
            </div>
            <div className="p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-100 dark:border-amber-800">
              <div className="text-xs text-amber-600 dark:text-amber-400 font-medium">Pipeline Stages</div>
              <div className="text-lg font-bold text-amber-800 dark:text-amber-300">{result.pipeline_stages.length}</div>
            </div>
          </div>

          {/* Pipeline Stages */}
          <div className="flex flex-wrap gap-2">
            {result.pipeline_stages.map((stage, i) => (
              <span
                key={stage}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-medium
                           bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300"
              >
                <span className="w-4 h-4 flex items-center justify-center rounded-full bg-[#8B5CF6] text-white text-[10px]">{i + 1}</span>
                {stage.replace(/_/g, ' ')}
              </span>
            ))}
          </div>

          {/* Config */}
          <details className="text-sm">
            <summary className="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
              Pipeline Config
            </summary>
            <pre className="mt-2 p-3 rounded-lg bg-gray-50 dark:bg-gray-800 text-xs overflow-x-auto">
              {JSON.stringify(result.config, null, 2)}
            </pre>
          </details>

          {/* Results with Scoring Breakdown */}
          <div className="space-y-3">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
              Results ({result.results.length})
            </h3>

            {result.results.map((mem, idx) => (
              <ResultCard
                key={mem.memory_id}
                rank={idx + 1}
                mem={mem}
                expanded={expandedId === mem.memory_id}
                onToggle={() => setExpandedId(expandedId === mem.memory_id ? null : mem.memory_id)}
                scoreColor={scoreColor}
                barWidth={barWidth}
                barColor={barColor}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function ResultCard({
  rank, mem, expanded, onToggle, scoreColor, barWidth, barColor
}: {
  rank: number;
  mem: ScoringBreakdown;
  expanded: boolean;
  onToggle: () => void;
  scoreColor: (s: number) => string;
  barWidth: (s: number) => string;
  barColor: (s: number) => string;
}) {
  const scores = [
    { label: 'Semantic', value: mem.semantic_score, icon: Search },
    { label: 'Recency', value: mem.recency_score, icon: Clock },
    { label: 'Entity', value: mem.entity_score, icon: Users },
    { label: 'Keyword', value: mem.keyword_score, icon: Type },
  ];

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 p-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50"
      >
        <span className="flex-shrink-0 w-6 h-6 flex items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900 text-#7C3AED dark:text-blue-300 text-xs font-bold">
          {rank}
        </span>
        <div className="flex-1 min-w-0">
          <p className="text-sm text-gray-900 dark:text-white truncate">{mem.content}</p>
          {mem.age_days != null && (
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              {mem.age_days < 0 ? 'Just now' : mem.age_days < 1 ? 'Today' : `${mem.age_days.toFixed(0)} days ago`}
            </p>
          )}
        </div>
        <span className={clsx('text-sm font-bold', scoreColor(mem.final_score))}>
          {(mem.final_score * 100).toFixed(1)}%
        </span>
        {expanded ? <ChevronDown className="w-4 h-4 text-gray-400" /> : <ChevronRight className="w-4 h-4 text-gray-400" />}
      </button>

      {expanded && (
        <div className="px-3 pb-3 pt-1 border-t border-gray-100 dark:border-gray-700 space-y-2">
          {scores.map(({ label, value, icon: Icon }) => (
            <div key={label} className="flex items-center gap-2">
              <Icon className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
              <span className="text-xs text-gray-500 dark:text-gray-400 w-16">{label}</span>
              <div className="flex-1 h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full transition-all', barColor(value))}
                  style={{ width: barWidth(value) }}
                />
              </div>
              <span className={clsx('text-xs font-mono w-12 text-right', scoreColor(value))}>
                {(value * 100).toFixed(1)}%
              </span>
            </div>
          ))}

          {mem.rerank_score != null && (
            <div className="flex items-center gap-2">
              <Zap className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
              <span className="text-xs text-gray-500 dark:text-gray-400 w-16">Rerank</span>
              <div className="flex-1 h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
                <div
                  className={clsx('h-full rounded-full', barColor(mem.rerank_score))}
                  style={{ width: barWidth(mem.rerank_score) }}
                />
              </div>
              <span className={clsx('text-xs font-mono w-12 text-right', scoreColor(mem.rerank_score))}>
                {(mem.rerank_score * 100).toFixed(1)}%
              </span>
            </div>
          )}

          <div className="pt-1 text-xs text-gray-500 dark:text-gray-400">
            <span className="font-mono">{mem.memory_id.slice(0, 8)}...</span>
          </div>
        </div>
      )}
    </div>
  );
}
