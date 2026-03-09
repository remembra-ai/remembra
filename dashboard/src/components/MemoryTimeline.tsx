import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import type { MemoryTimelineResponse, TimelineMemory } from '../lib/api';
import { Clock, Eye, Tag, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';
import clsx from 'clsx';

const entityColors: Record<string, string> = {
  person: 'bg-blue-100 dark:bg-blue-900/40 text-#7C3AED dark:text-blue-300',
  company: 'bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300',
  organization: 'bg-purple-100 dark:bg-purple-900/40 text-purple-700 dark:text-purple-300',
  location: 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-300',
  concept: 'bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300',
};

export function MemoryTimeline() {
  const [data, setData] = useState<MemoryTimelineResponse | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadTimeline();
  }, [page]);

  const loadTimeline = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMemoryTimeline(page, 30);
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load timeline');
    } finally {
      setLoading(false);
    }
  };

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  const formatDate = (iso: string) => {
    const d = new Date(iso);
    const now = new Date();
    const diff = now.getTime() - d.getTime();
    const days = Math.floor(diff / 86400000);

    // Handle edge case where server time is slightly ahead (negative diff)
    if (days < 0) return 'Just now';
    if (days === 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 7) return `${days} days ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  };

  const formatTime = (iso: string) => {
    return new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
  };

  if (error) {
    return (
      <div className="p-4 rounded-lg bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400">
        {error}
      </div>
    );
  }

  // Group memories by date
  const grouped: Record<string, TimelineMemory[]> = {};
  if (data) {
    for (const mem of data.memories) {
      const dateKey = formatDate(mem.created_at);
      if (!grouped[dateKey]) grouped[dateKey] = [];
      grouped[dateKey].push(mem);
    }
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
          {data ? `${data.total} memories` : 'Loading...'}
        </h3>

        {totalPages > 1 && (
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage(Math.max(1, page - 1))}
              disabled={page <= 1}
              className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {page} / {totalPages}
            </span>
            <button
              onClick={() => setPage(Math.min(totalPages, page + 1))}
              disabled={page >= totalPages}
              className="p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
            >
              <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-6 h-6 animate-spin text-[#8B5CF6]" />
        </div>
      ) : (
        <div className="relative">
          {/* Timeline line */}
          <div className="absolute left-4 top-0 bottom-0 w-0.5 bg-gray-200 dark:bg-gray-700" />

          {Object.entries(grouped).map(([dateLabel, memories]) => (
            <div key={dateLabel} className="mb-6">
              {/* Date header */}
              <div className="relative flex items-center mb-3 pl-10">
                <div className="absolute left-2.5 w-3 h-3 rounded-full bg-[#8B5CF6] ring-4 ring-white dark:ring-gray-900" />
                <span className="text-sm font-semibold text-gray-700 dark:text-gray-300">{dateLabel}</span>
              </div>

              {/* Memory items */}
              <div className="space-y-2 pl-10">
                {memories.map((mem) => (
                  <div
                    key={mem.id}
                    className="p-3 rounded-lg border border-gray-200 dark:border-gray-700
                               bg-white dark:bg-gray-800/50 hover:border-blue-300 dark:hover:border-#7C3AED
                               transition-colors"
                  >
                    <p className="text-sm text-gray-900 dark:text-white line-clamp-2">{mem.content}</p>

                    <div className="flex items-center gap-3 mt-2 flex-wrap">
                      <span className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
                        <Clock className="w-3 h-3" />
                        {formatTime(mem.created_at)}
                      </span>

                      {mem.access_count > 0 && (
                        <span className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
                          <Eye className="w-3 h-3" />
                          {mem.access_count}x
                        </span>
                      )}

                      {mem.entities.map((entity, i) => (
                        <span
                          key={i}
                          className={clsx(
                            'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium',
                            entityColors[entity.type] || 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-400'
                          )}
                        >
                          <Tag className="w-2.5 h-2.5" />
                          {entity.name}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
