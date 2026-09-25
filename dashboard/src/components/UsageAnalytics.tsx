import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { AnalyticsResponse } from '../lib/api';
import { Database, Users, GitBranch, Activity, Heart, AlertTriangle, XCircle, Loader2 } from 'lucide-react';
import clsx from 'clsx';

interface UsageAnalyticsProps {
  projectId?: string;
}

const statCardStyles = {
  blue: {
    card: 'bg-blue-50 dark:bg-blue-900/20 border-blue-100 dark:border-blue-800',
    icon: 'text-blue-600 dark:text-blue-400',
    label: 'text-blue-600 dark:text-blue-400',
    value: 'text-blue-800 dark:text-blue-300',
  },
  purple: {
    card: 'bg-purple-50 dark:bg-purple-900/20 border-purple-100 dark:border-purple-800',
    icon: 'text-purple-600 dark:text-purple-400',
    label: 'text-purple-600 dark:text-purple-400',
    value: 'text-purple-800 dark:text-purple-300',
  },
  green: {
    card: 'bg-green-50 dark:bg-green-900/20 border-green-100 dark:border-green-800',
    icon: 'text-green-600 dark:text-green-400',
    label: 'text-green-600 dark:text-green-400',
    value: 'text-green-800 dark:text-green-300',
  },
  amber: {
    card: 'bg-amber-50 dark:bg-amber-900/20 border-amber-100 dark:border-amber-800',
    icon: 'text-amber-600 dark:text-amber-400',
    label: 'text-amber-600 dark:text-amber-400',
    value: 'text-amber-800 dark:text-amber-300',
  },
} as const;

type StatTone = keyof typeof statCardStyles;

export function UsageAnalytics({ projectId }: UsageAnalyticsProps) {
  const [analytics, setAnalytics] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadAnalytics();
  }, [projectId]);

  const loadAnalytics = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getAnalytics(projectId);
      setAnalytics(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load analytics');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="w-6 h-6 animate-spin text-signal-ink" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400">
        {error}
      </div>
    );
  }

  if (!analytics) return null;

  const statCards: Array<{ label: string; value: string; icon: typeof Database; color: StatTone }> = [
    { label: 'Memories', value: analytics.total_memories.toLocaleString(), icon: Database, color: 'blue' },
    { label: 'Entities', value: analytics.total_entities.toLocaleString(), icon: Users, color: 'purple' },
    { label: 'Relationships', value: analytics.total_relationships.toLocaleString(), icon: GitBranch, color: 'green' },
    { label: 'Avg Decay', value: `${(analytics.avg_decay_score * 100).toFixed(0)}%`, icon: Activity, color: 'amber' },
  ];

  const healthTotal = analytics.healthy_memories + analytics.stale_memories + analytics.critical_memories;
  const healthPct = (count: number) => healthTotal > 0 ? Math.round((count / healthTotal) * 100) : 0;

  const entityTypeColors: Record<string, string> = {
    person: 'bg-accent',
    company: 'bg-purple-500',
    organization: 'bg-purple-500',
    location: 'bg-green-500',
    concept: 'bg-amber-500',
  };

  return (
    <div className="space-y-6">
      {/* Overview Cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {statCards.map(({ label, value, icon: Icon, color }) => (
          <div
            key={label}
            className={clsx(
              'p-4 rounded-lg border',
              statCardStyles[color].card
            )}
          >
            <div className="flex items-center gap-2 mb-1">
              <Icon className={clsx('w-4 h-4', statCardStyles[color].icon)} />
              <span className={clsx('text-xs font-medium', statCardStyles[color].label)}>{label}</span>
            </div>
            <div className={clsx('text-2xl font-bold', statCardStyles[color].value)}>{value}</div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Memory Health */}
        <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Memory Health</h3>

          {healthTotal === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">No decay data available</p>
          ) : (
            <div className="space-y-3">
              {/* Stacked bar */}
              <div className="h-4 rounded-full overflow-hidden flex bg-gray-100 dark:bg-gray-700">
                {analytics.healthy_memories > 0 && (
                  <div
                    className="bg-green-500 transition-all"
                    style={{ width: `${healthPct(analytics.healthy_memories)}%` }}
                    title={`Healthy: ${analytics.healthy_memories}`}
                  />
                )}
                {analytics.stale_memories > 0 && (
                  <div
                    className="bg-yellow-500 transition-all"
                    style={{ width: `${healthPct(analytics.stale_memories)}%` }}
                    title={`Stale: ${analytics.stale_memories}`}
                  />
                )}
                {analytics.critical_memories > 0 && (
                  <div
                    className="bg-red-500 transition-all"
                    style={{ width: `${healthPct(analytics.critical_memories)}%` }}
                    title={`Critical: ${analytics.critical_memories}`}
                  />
                )}
              </div>

              <div className="flex justify-between text-xs">
                <span className="flex items-center gap-1 text-green-600 dark:text-green-400">
                  <Heart className="w-3 h-3" /> Healthy {analytics.healthy_memories}
                </span>
                <span className="flex items-center gap-1 text-yellow-600 dark:text-yellow-400">
                  <AlertTriangle className="w-3 h-3" /> Stale {analytics.stale_memories}
                </span>
                <span className="flex items-center gap-1 text-red-600 dark:text-red-400">
                  <XCircle className="w-3 h-3" /> Critical {analytics.critical_memories}
                </span>
              </div>
            </div>
          )}
        </div>

        {/* Entity Types */}
        <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Entity Distribution</h3>

          {Object.keys(analytics.entities_by_type).length === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">No entities yet</p>
          ) : (
            <div className="space-y-2">
              {Object.entries(analytics.entities_by_type)
                .sort(([, a], [, b]) => b - a)
                .map(([type, count]) => (
                  <div key={type} className="flex items-center gap-2">
                    <span className={clsx('w-2.5 h-2.5 rounded-full', entityTypeColors[type] || 'bg-gray-400')} />
                    <span className="text-sm text-gray-700 dark:text-gray-300 capitalize flex-1">{type}</span>
                    <span className="text-sm font-mono text-gray-500 dark:text-gray-400">{count}</span>
                  </div>
                ))}
            </div>
          )}
        </div>

        {/* Memory Age Distribution */}
        <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Memory Age</h3>

          <div className="space-y-2">
            {Object.entries(analytics.age_distribution).map(([bucket, count]) => {
              const total = Object.values(analytics.age_distribution).reduce((a, b) => a + b, 0);
              const pct = total > 0 ? Math.round((count / total) * 100) : 0;
              const labels: Record<string, string> = {
                today: 'Today',
                this_week: 'This Week',
                this_month: 'This Month',
                older: 'Older',
              };

              return (
                <div key={bucket} className="flex items-center gap-2">
                  <span className="text-xs text-gray-500 dark:text-gray-400 w-20">{labels[bucket] || bucket}</span>
                  <div className="flex-1 h-3 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-accent rounded-full transition-all"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono text-gray-500 dark:text-gray-400 w-10 text-right">{count}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Today's Activity */}
        <div className="p-4 rounded-lg border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Today's Activity</h3>

          <div className="grid grid-cols-2 gap-4">
            <div className="text-center">
              <div className="text-2xl font-bold text-signal-ink dark:text-signal-ink">{analytics.stores_today}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Stores</div>
            </div>
            <div className="text-center">
              <div className="text-2xl font-bold text-purple-600 dark:text-purple-400">{analytics.recalls_today}</div>
              <div className="text-xs text-gray-500 dark:text-gray-400">Recalls</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
