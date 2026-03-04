import { User, Tag, ChevronRight, Clock, Sparkles } from 'lucide-react';
import type { Memory } from '../lib/api';
import clsx from 'clsx';

interface MemoryCardProps {
  memory: Memory;
  onClick?: () => void;
  showRelevance?: boolean;
  compact?: boolean;
}

export function MemoryCard({ memory, onClick, showRelevance = false, compact = false }: MemoryCardProps) {
  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    
    return date.toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
    });
  };

  const truncateContent = (content: string, maxLength = compact ? 100 : 180) => {
    if (content.length <= maxLength) return content;
    return content.slice(0, maxLength).trim() + '...';
  };

  const getRelevanceStyles = (score: number) => {
    if (score >= 0.8) return { 
      bar: 'bg-green-400', 
      text: 'text-green-400',
      width: '100%'
    };
    if (score >= 0.6) return { 
      bar: 'bg-[#8B5CF6]', 
      text: 'text-[#A78BFA]',
      width: '75%'
    };
    if (score >= 0.4) return { 
      bar: 'bg-amber-400', 
      text: 'text-amber-400',
      width: '50%'
    };
    return { 
      bar: 'bg-[hsl(var(--muted-foreground))]', 
      text: 'text-[hsl(var(--muted-foreground))]',
      width: '25%'
    };
  };

  const relevance = memory.relevance ?? 0;
  const relevanceStyles = getRelevanceStyles(relevance);

  return (
    <div
      onClick={onClick}
      className={clsx(
        'group relative',
        'bg-[hsl(var(--card))] rounded-xl',
        'border border-[hsl(var(--border))]',
        'transition-all duration-200 ease-out',
        onClick && [
          'cursor-pointer',
          'hover:border-[#8B5CF6]/40',
          'hover:shadow-lg hover:shadow-purple-500/5',
          'hover:translate-y-[-1px]'
        ],
        compact ? 'p-3' : 'p-4'
      )}
    >
      {/* Hover gradient overlay */}
      <div className="absolute inset-0 rounded-xl bg-gradient-to-r from-[#8B5CF6]/5 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none" />
      
      {/* Active indicator */}
      <div className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-8 bg-[#8B5CF6] rounded-r opacity-0 group-hover:opacity-100 transition-opacity" />

      <div className="relative">
        {/* Content */}
        <div className="flex items-start gap-3">
          <div className="flex-1 min-w-0">
            <p className={clsx(
              'text-[hsl(var(--foreground))] leading-relaxed',
              compact ? 'text-sm' : 'text-[15px]'
            )}>
              {truncateContent(memory.content)}
            </p>
          </div>
          
          {onClick && (
            <ChevronRight className="w-4 h-4 text-[hsl(var(--muted-foreground))] opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0 mt-1" />
          )}
        </div>

        {/* Entity Tags */}
        {memory.entities && memory.entities.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mt-3">
            {memory.entities.slice(0, 4).map((entity, index) => (
              <span
                key={index}
                className={clsx(
                  'inline-flex items-center gap-1 px-2 py-0.5 rounded-md',
                  'text-xs font-medium',
                  'bg-[#8B5CF6]/10 text-[#A78BFA]',
                  'border border-[#8B5CF6]/20'
                )}
              >
                <User className="w-3 h-3" />
                {entity}
              </span>
            ))}
            {memory.entities.length > 4 && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-md text-xs font-medium text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))]">
                +{memory.entities.length - 4}
              </span>
            )}
          </div>
        )}

        {/* Footer: Metadata + Relevance */}
        <div className={clsx(
          'flex items-center justify-between gap-4',
          compact ? 'mt-2' : 'mt-3',
          'text-xs text-[hsl(var(--muted-foreground))]'
        )}>
          <div className="flex items-center gap-3">
            {/* Timestamp */}
            <div className="flex items-center gap-1">
              <Clock className="w-3 h-3" />
              <span>{formatDate(memory.created_at)}</span>
            </div>

            {/* Memory Type */}
            {memory.memory_type && (
              <div className="flex items-center gap-1">
                <Tag className="w-3 h-3" />
                <span className="capitalize">{memory.memory_type}</span>
              </div>
            )}

            {/* Access Count */}
            {memory.access_count !== undefined && memory.access_count > 0 && (
              <div className="flex items-center gap-1">
                <Sparkles className="w-3 h-3" />
                <span>{memory.access_count} recalls</span>
              </div>
            )}
          </div>

          {/* Relevance Score */}
          {showRelevance && memory.relevance !== undefined && (
            <div className="flex items-center gap-2">
              <div className="w-16 h-1.5 bg-[hsl(var(--muted))] rounded-full overflow-hidden">
                <div 
                  className={clsx('h-full rounded-full transition-all', relevanceStyles.bar)}
                  style={{ width: `${relevance * 100}%` }}
                />
              </div>
              <span className={clsx('text-xs font-medium tabular-nums', relevanceStyles.text)}>
                {(relevance * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
