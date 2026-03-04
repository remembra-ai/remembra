import { useState, useEffect } from 'react';
import { useMemories, useSearch } from '../hooks/useMemories';
import { SearchBar } from '../components/SearchBar';
import { MemoryList } from '../components/MemoryList';
import { MemoryDetail } from './MemoryDetail';
import { DecayReport } from '../components/DecayReport';
import { StoreMemory } from '../components/StoreMemory';
import { EntityList } from '../components/EntityList';
import { EntityGraph } from '../components/EntityGraph';
import { QueryDebugger } from '../components/QueryDebugger';
import { UsageAnalytics } from '../components/UsageAnalytics';
import { MemoryTimeline } from '../components/MemoryTimeline';
import { ApiKeyManager } from '../components/ApiKeyManager';
import { Billing } from '../components/Billing';
import { Settings } from './Settings';
import { StatsOverview } from '../components/StatsOverview';
import { EmptyState } from '../components/EmptyState';
import { type Memory } from '../lib/api';
import { RefreshCw, Plus } from 'lucide-react';
import clsx from 'clsx';

export type TabType = 'memories' | 'entities' | 'graph' | 'decay' | 'debugger' | 'analytics' | 'timeline' | 'keys' | 'billing' | 'settings';

interface DashboardProps {
  activeTab: TabType;
  onLogout?: () => void;
}

export function Dashboard({ activeTab, onLogout }: DashboardProps) {
  const { memories, loading, error, hasMore, refresh, loadMore } = useMemories();
  const { results, loading: searchLoading, error: searchError, search, clear } = useSearch();
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [showNewMemory, setShowNewMemory] = useState(false);

  // Stats (placeholder - would come from API in real implementation)
  const [stats, setStats] = useState({
    memoryCount: 0,
    entityCount: 0,
    storageUsed: '0 MB',
    apiCalls: 0,
  });

  // Update stats when memories change
  useEffect(() => {
    if (memories.length > 0) {
      const uniqueEntities = new Set<string>();
      memories.forEach(m => m.entities?.forEach(e => uniqueEntities.add(e)));
      
      setStats({
        memoryCount: memories.length + (hasMore ? 100 : 0), // Estimate if more
        entityCount: uniqueEntities.size,
        storageUsed: `${(memories.length * 0.5).toFixed(1)} KB`, // Rough estimate
        apiCalls: Math.floor(Math.random() * 10000) + 1000, // Placeholder
      });
    }
  }, [memories, hasMore]);

  const handleSearch = (query: string) => {
    setIsSearching(true);
    search(query);
  };

  const handleClearSearch = () => {
    setIsSearching(false);
    clear();
  };

  const handleSelectMemory = (memory: Memory) => {
    setSelectedMemory(memory);
  };

  const handleCloseDetail = () => {
    setSelectedMemory(null);
  };

  // If a memory is selected, show the detail view
  if (selectedMemory) {
    return (
      <MemoryDetail
        memory={selectedMemory}
        onClose={handleCloseDetail}
        onDelete={() => {
          handleCloseDetail();
          refresh();
        }}
      />
    );
  }

  // New memory modal
  if (showNewMemory) {
    return (
      <div>
        <button
          onClick={() => setShowNewMemory(false)}
          className="mb-4 text-sm text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
        >
          ← Back to memories
        </button>
        <StoreMemory onStored={() => {
          setShowNewMemory(false);
          refresh();
        }} />
      </div>
    );
  }

  const displayMemories = isSearching && results ? results.memories : memories;
  const displayLoading = isSearching ? searchLoading : loading;
  const displayError = isSearching ? searchError : error;

  const renderContent = () => {
    switch (activeTab) {
      case 'memories':
        return (
          <>
            {/* Stats Overview */}
            <StatsOverview
              memoryCount={stats.memoryCount}
              entityCount={stats.entityCount}
              storageUsed={stats.storageUsed}
              apiCalls={stats.apiCalls}
              loading={loading && memories.length === 0}
            />

            {/* Search & Actions Bar */}
            <div className="flex flex-col sm:flex-row gap-4 mb-6">
              <div className="flex-1">
                <SearchBar
                  onSearch={handleSearch}
                  onClear={handleClearSearch}
                  loading={searchLoading}
                />
              </div>
              
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setShowNewMemory(true)}
                  className={clsx(
                    'inline-flex items-center gap-2 px-4 py-2.5 rounded-lg',
                    'bg-[#8B5CF6] hover:bg-[#7C3AED] text-white',
                    'font-medium text-sm transition-colors',
                    'shadow-lg shadow-purple-500/20'
                  )}
                >
                  <Plus className="w-4 h-4" />
                  New Memory
                </button>
                
                <button
                  onClick={refresh}
                  disabled={loading}
                  className={clsx(
                    'p-2.5 rounded-lg',
                    'bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80',
                    'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                    'transition-colors',
                    loading && 'opacity-50 cursor-not-allowed'
                  )}
                  title="Refresh"
                >
                  <RefreshCw className={clsx('w-5 h-5', loading && 'animate-spin')} />
                </button>
              </div>
            </div>

            {/* Results Header */}
            <div className="flex items-center justify-between mb-4">
              <div>
                {isSearching && results ? (
                  <h2 className="text-lg font-semibold text-[hsl(var(--foreground))]">
                    {results.memories.length} result{results.memories.length !== 1 ? 's' : ''} 
                    <span className="font-normal text-[hsl(var(--muted-foreground))]"> for "{results.query}"</span>
                  </h2>
                ) : (
                  <h2 className="text-lg font-semibold text-[hsl(var(--foreground))]">
                    All Memories
                    {memories.length > 0 && (
                      <span className="ml-2 text-sm font-normal text-[hsl(var(--muted-foreground))]">
                        ({memories.length}{hasMore ? '+' : ''})
                      </span>
                    )}
                  </h2>
                )}
              </div>
            </div>

            {/* Context (for search results) */}
            {isSearching && results?.context && (
              <div className={clsx(
                'mb-6 p-4 rounded-xl',
                'bg-[#8B5CF6]/10 border border-[#8B5CF6]/20'
              )}>
                <h3 className="text-sm font-medium text-[#A78BFA] mb-2">
                  Context Summary
                </h3>
                <p className="text-sm text-[hsl(var(--foreground))]">
                  {results.context}
                </p>
              </div>
            )}

            {/* Error State */}
            {displayError && (
              <div className={clsx(
                'mb-6 p-4 rounded-xl',
                'bg-red-500/10 border border-red-500/20',
                'text-red-400'
              )}>
                {displayError}
              </div>
            )}

            {/* Memory List or Empty State */}
            {displayMemories.length > 0 ? (
              <MemoryList
                memories={displayMemories}
                loading={displayLoading}
                error={displayError}
                onSelectMemory={handleSelectMemory}
                showRelevance={isSearching}
              />
            ) : !displayLoading ? (
              <EmptyState 
                type={isSearching ? 'search' : 'memories'}
                searchQuery={isSearching ? results?.query : undefined}
                onAction={isSearching ? undefined : () => setShowNewMemory(true)}
                actionLabel={isSearching ? undefined : 'Store First Memory'}
              />
            ) : null}

            {/* Load More */}
            {!isSearching && hasMore && memories.length > 0 && (
              <div className="mt-6 text-center">
                <button
                  onClick={loadMore}
                  disabled={loading}
                  className={clsx(
                    'px-6 py-2.5 rounded-lg',
                    'bg-[hsl(var(--muted))] hover:bg-[hsl(var(--muted))]/80',
                    'text-[hsl(var(--foreground))] font-medium text-sm',
                    'transition-colors',
                    loading && 'opacity-50 cursor-not-allowed'
                  )}
                >
                  {loading ? 'Loading...' : 'Load More'}
                </button>
              </div>
            )}
          </>
        );

      case 'entities':
        return <EntityList />;

      case 'graph':
        return <EntityGraph />;

      case 'decay':
        return <DecayReport />;

      case 'debugger':
        return <QueryDebugger />;

      case 'analytics':
        return <UsageAnalytics />;

      case 'timeline':
        return <MemoryTimeline />;

      case 'keys':
        return <ApiKeyManager />;

      case 'billing':
        return <Billing />;

      case 'settings':
        return <Settings onLogout={onLogout || (() => {})} />;

      default:
        return null;
    }
  };

  return (
    <div className="min-h-full">
      {renderContent()}
    </div>
  );
}
