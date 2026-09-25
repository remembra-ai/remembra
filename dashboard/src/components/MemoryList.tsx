import { motion } from 'framer-motion';
import type { Memory } from '../lib/api';
import { MemoryCard } from './MemoryCard';
import { Brain } from 'lucide-react';
import { staggerContainer } from '../lib/motion';
import { MemoryListSkeleton } from './Skeleton';

interface MemoryListProps {
  memories: Memory[];
  loading: boolean;
  error: string | null;
  hasMore?: boolean;
  onLoadMore?: () => void;
  onSelectMemory?: (memory: Memory) => void;
  showRelevance?: boolean;
  emptyMessage?: string;
}

export function MemoryList({
  memories,
  loading,
  error,
  hasMore = false,
  onLoadMore,
  onSelectMemory,
  showRelevance = false,
  emptyMessage = 'No memories found',
}: MemoryListProps) {
  if (error) {
    return (
      <div className="flex flex-col items-center justify-center p-8 text-center">
        <div className="w-12 h-12 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center mb-3">
          <span className="text-red-600 dark:text-red-400 text-xl">!</span>
        </div>
        <p className="text-red-600 dark:text-red-400 font-medium">{error}</p>
      </div>
    );
  }

  if (loading && memories.length === 0) {
    return <MemoryListSkeleton count={5} />;
  }

  if (!loading && memories.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-12 text-center">
        <div className="w-16 h-16 rounded-full bg-gray-100 dark:bg-gray-800 flex items-center justify-center mb-4">
          <Brain className="w-8 h-8 text-gray-400" />
        </div>
        <p className="text-gray-500 dark:text-gray-400">{emptyMessage}</p>
      </div>
    );
  }

  return (
    <motion.div
      className="space-y-3"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      {memories.map((memory) => (
        <MemoryCard
          key={memory.id}
          memory={memory}
          onClick={onSelectMemory ? () => onSelectMemory(memory) : undefined}
          showRelevance={showRelevance}
        />
      ))}

      {loading && (
        <MemoryListSkeleton count={3} />
      )}

      {!loading && hasMore && onLoadMore && (
        <motion.button
          onClick={onLoadMore}
          whileHover={{ scale: 1.01 }}
          whileTap={{ scale: 0.99 }}
          className="w-full py-3 text-sm font-medium text-signal-ink dark:text-signal-ink hover:bg-[hsl(var(--muted))] rounded-lg transition-colors"
        >
          Load more
        </motion.button>
      )}
    </motion.div>
  );
}
