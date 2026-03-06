import { useState, useEffect } from 'react';
import { api } from '../lib/api';
import type { DecayReportResponse, MemoryDecayInfo } from '../lib/api';
import { AlertTriangle, RefreshCw, Trash2, Clock, Activity, TrendingDown } from 'lucide-react';
import clsx from 'clsx';

interface DecayReportProps {
  projectId?: string;
}

export function DecayReport({ projectId }: DecayReportProps) {
  const [report, setReport] = useState<DecayReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const [cleanupResult, setCleanupResult] = useState<string | null>(null);

  const fetchReport = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getDecayReport(projectId, 100);
      setReport(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load report');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchReport();
  }, [projectId]);

  const handleCleanup = async (dryRun: boolean) => {
    setCleaning(true);
    setCleanupResult(null);
    try {
      const result = await api.runCleanup(projectId, dryRun, false);
      if (dryRun) {
        setCleanupResult(
          `Dry run: Would delete ${result.expired_found} expired memories`
        );
      } else {
        setCleanupResult(
          `Cleaned up ${result.expired_deleted} expired memories in ${result.duration_ms}ms`
        );
        fetchReport(); // Refresh after cleanup
      }
    } catch (err) {
      setCleanupResult(err instanceof Error ? err.message : 'Cleanup failed');
    } finally {
      setCleaning(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <RefreshCw className="w-6 h-6 animate-spin text-gray-400" />
        <span className="ml-2 text-gray-500">Loading decay report...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-lg bg-red-50 dark:bg-red-900/20 p-4">
        <div className="flex items-center">
          <AlertTriangle className="w-5 h-5 text-red-500" />
          <span className="ml-2 text-red-700 dark:text-red-300">{error}</span>
        </div>
      </div>
    );
  }

  if (!report) return null;

  return (
    <div className="space-y-6">
      {/* Summary Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          icon={<Activity className="w-5 h-5" />}
          label="Total Memories"
          value={report.total_memories}
          color="blue"
        />
        <StatCard
          icon={<TrendingDown className="w-5 h-5" />}
          label="Avg Relevance"
          value={`${(report.average_relevance * 100).toFixed(1)}%`}
          color={report.average_relevance >= 0.5 ? 'green' : 'yellow'}
        />
        <StatCard
          icon={<AlertTriangle className="w-5 h-5" />}
          label="Prune Candidates"
          value={report.prune_candidates}
          color={report.prune_candidates > 0 ? 'red' : 'green'}
        />
        <StatCard
          icon={<Clock className="w-5 h-5" />}
          label="Decay Rate"
          value={`${(report.config.base_decay_rate * 100).toFixed(0)}%/day`}
          color="gray"
        />
      </div>

      {/* Cleanup Actions */}
      <div className="flex items-center gap-4 p-4 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <button
          onClick={() => handleCleanup(true)}
          disabled={cleaning}
          className={clsx(
            'px-4 py-2 rounded-lg font-medium transition-colors',
            'bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600',
            'text-gray-700 dark:text-gray-200',
            cleaning && 'opacity-50 cursor-not-allowed'
          )}
        >
          {cleaning ? 'Running...' : 'Preview Cleanup'}
        </button>
        <button
          onClick={() => handleCleanup(false)}
          disabled={cleaning || report.prune_candidates === 0}
          className={clsx(
            'px-4 py-2 rounded-lg font-medium transition-colors',
            'bg-red-500 hover:bg-red-600 text-white',
            (cleaning || report.prune_candidates === 0) && 'opacity-50 cursor-not-allowed'
          )}
        >
          <Trash2 className="w-4 h-4 inline mr-2" />
          Clean Expired
        </button>
        <button
          onClick={fetchReport}
          disabled={loading}
          className="p-2 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700"
        >
          <RefreshCw className={clsx('w-5 h-5 text-gray-500', loading && 'animate-spin')} />
        </button>
        {cleanupResult && (
          <span className="text-sm text-gray-600 dark:text-gray-400">
            {cleanupResult}
          </span>
        )}
      </div>

      {/* Memory List */}
      <div className="space-y-2">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
          Memories by Relevance (lowest first)
        </h3>
        <div className="divide-y divide-gray-200 dark:divide-gray-700 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
          {report.memories.map((memory) => (
            <MemoryDecayRow key={memory.id} memory={memory} />
          ))}
        </div>
      </div>

      {/* Config Info */}
      <div className="text-xs text-gray-500 dark:text-gray-400 p-3 bg-gray-50 dark:bg-gray-800 rounded-lg">
        <strong>Decay Config:</strong> Prune threshold: {(report.config.prune_threshold * 100).toFixed(0)}% | 
        Base decay: {(report.config.base_decay_rate * 100).toFixed(0)}%/day | 
        Grace period: {report.config.newness_grace_days} days
      </div>
    </div>
  );
}

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string | number;
  color: 'blue' | 'green' | 'yellow' | 'red' | 'gray';
}

function StatCard({ icon, label, value, color }: StatCardProps) {
  const colorClasses = {
    blue: 'bg-blue-50 dark:bg-blue-900/20 text-[#8B5CF6] dark:text-[#A78BFA]',
    green: 'bg-green-50 dark:bg-green-900/20 text-green-600 dark:text-green-400',
    yellow: 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600 dark:text-yellow-400',
    red: 'bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-400',
    gray: 'bg-gray-50 dark:bg-gray-800 text-gray-600 dark:text-gray-400',
  };

  return (
    <div className={clsx('rounded-lg p-4', colorClasses[color])}>
      <div className="flex items-center gap-2 mb-1">
        {icon}
        <span className="text-sm font-medium opacity-80">{label}</span>
      </div>
      <div className="text-2xl font-bold">{value}</div>
    </div>
  );
}

interface MemoryDecayRowProps {
  memory: MemoryDecayInfo;
}

function MemoryDecayRow({ memory }: MemoryDecayRowProps) {
  const { width, bgColor } = getRelevanceBarStyle(memory.relevance_score);

  return (
    <div className={clsx(
      'p-4 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors',
      memory.should_prune && 'bg-red-50 dark:bg-red-900/10'
    )}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <p className="text-sm text-gray-900 dark:text-white truncate">
            {memory.content_preview}
          </p>
          <div className="flex items-center gap-4 mt-2 text-xs text-gray-500 dark:text-gray-400">
            <span>
              <Clock className="w-3 h-3 inline mr-1" />
              {memory.days_since_access.toFixed(1)}d ago
            </span>
            <span>
              <Activity className="w-3 h-3 inline mr-1" />
              {memory.access_count} accesses
            </span>
            <span>Stability: {memory.stability.toFixed(1)}</span>
            {memory.ttl_remaining_seconds !== null && (
              <span className="text-orange-500">
                TTL: {formatTTL(memory.ttl_remaining_seconds)}
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className={clsx(
            'text-lg font-bold',
            getRelevanceColor(memory.relevance_score)
          )}>
            {(memory.relevance_score * 100).toFixed(0)}%
          </span>
          <div className="w-20 h-2 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
            <div
              className={clsx('h-full rounded-full transition-all', bgColor)}
              style={{ width }}
            />
          </div>
          {memory.should_prune && (
            <span className="text-xs text-red-500 font-medium">PRUNE</span>
          )}
        </div>
      </div>
    </div>
  );
}

function getRelevanceColor(score: number): string {
  if (score >= 0.7) return 'text-green-600 dark:text-green-400';
  if (score >= 0.4) return 'text-yellow-600 dark:text-yellow-400';
  if (score >= 0.1) return 'text-orange-600 dark:text-orange-400';
  return 'text-red-600 dark:text-red-400';
}

function getRelevanceBarStyle(score: number): { width: string; bgColor: string } {
  const width = `${Math.max(5, Math.min(100, score * 100))}%`;
  let bgColor = 'bg-green-500';
  if (score < 0.7) bgColor = 'bg-yellow-500';
  if (score < 0.4) bgColor = 'bg-orange-500';
  if (score < 0.1) bgColor = 'bg-red-500';
  return { width, bgColor };
}

function formatTTL(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(0)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(0)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}
