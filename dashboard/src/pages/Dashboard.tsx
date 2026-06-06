import { lazy, Suspense, useState, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useMemories, useSearch } from '../hooks/useMemories';
import { SearchBar } from '../components/SearchBar';
import { MemoryList } from '../components/MemoryList';
import { StatsOverview } from '../components/StatsOverview';
import { ControlPlaneOverview } from '../components/ControlPlaneOverview';
import { EmptyState } from '../components/EmptyState';
import { type Memory, api } from '../lib/api';
import { RefreshCw, Plus } from 'lucide-react';
import clsx from 'clsx';
import { pageTransition } from '../lib/motion';

const MemoryDetail = lazy(() =>
  import('./MemoryDetail').then((module) => ({ default: module.MemoryDetail })),
);
const DecayReport = lazy(() =>
  import('../components/DecayReport').then((module) => ({ default: module.DecayReport })),
);
const StoreMemory = lazy(() =>
  import('../components/StoreMemory').then((module) => ({ default: module.StoreMemory })),
);
const EntityList = lazy(() =>
  import('../components/EntityList').then((module) => ({ default: module.EntityList })),
);
const EntityGraph = lazy(() =>
  import('../components/EntityGraph').then((module) => ({ default: module.EntityGraph })),
);
const QueryDebugger = lazy(() =>
  import('../components/QueryDebugger').then((module) => ({ default: module.QueryDebugger })),
);
const UsageAnalytics = lazy(() =>
  import('../components/UsageAnalytics').then((module) => ({ default: module.UsageAnalytics })),
);
const MemoryTimeline = lazy(() =>
  import('../components/MemoryTimeline').then((module) => ({ default: module.MemoryTimeline })),
);
const ApiKeyManager = lazy(() =>
  import('../components/ApiKeyManager').then((module) => ({ default: module.ApiKeyManager })),
);
const Billing = lazy(() =>
  import('../components/Billing').then((module) => ({ default: module.Billing })),
);
const Teams = lazy(() =>
  import('../components/Teams').then((module) => ({ default: module.Teams })),
);
const Projects = lazy(() =>
  import('../components/Projects').then((module) => ({ default: module.Projects })),
);
const Settings = lazy(() =>
  import('./Settings').then((module) => ({ default: module.Settings })),
);
const Admin = lazy(() =>
  import('./Admin').then((module) => ({ default: module.Admin })),
);

export type TabType = 'memories' | 'entities' | 'graph' | 'decay' | 'debugger' | 'analytics' | 'timeline' | 'projects' | 'keys' | 'billing' | 'settings' | 'teams' | 'admin';

interface DashboardProps {
  activeTab: TabType;
  onLogout?: () => void;
  showNewMemory?: boolean;
  onCloseNewMemory?: () => void;
  onTabChange?: (tab: TabType) => void;
}

function SectionLoading({ label = 'Loading workspace surface...' }: { label?: string }) {
  return (
    <div className="flex min-h-[320px] items-center justify-center rounded-2xl dashboard-surface">
      <div className="text-center">
        <div className="mx-auto mb-3 h-10 w-10 animate-spin rounded-full border-2 border-[hsl(var(--border))] border-t-[hsl(var(--primary))]" />
        <p className="text-sm text-[hsl(var(--muted-foreground))]">{label}</p>
      </div>
    </div>
  );
}

export function Dashboard({ activeTab, onLogout, showNewMemory: showNewMemoryProp, onCloseNewMemory, onTabChange }: DashboardProps) {
  const [currentProjectId, setCurrentProjectId] = useState(() => api.getProjectId() || 'default');
  const { memories, loading, error, hasMore, refresh, loadMore, wsConnected } = useMemories(20, currentProjectId);
  const { results, loading: searchLoading, error: searchError, search, clear } = useSearch(currentProjectId);
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [showNewMemory, setShowNewMemory] = useState(false);

  useEffect(() => {
    const syncProject = () => {
      setCurrentProjectId(api.getProjectId() || 'default');
    };

    const handleProjectChanged = (event: Event) => {
      const customEvent = event as CustomEvent<{ projectId?: string }>;
      setCurrentProjectId(customEvent.detail?.projectId || api.getProjectId() || 'default');
    };

    syncProject();
    window.addEventListener('storage', syncProject);
    window.addEventListener('remembra:project-changed', handleProjectChanged as EventListener);

    return () => {
      window.removeEventListener('storage', syncProject);
      window.removeEventListener('remembra:project-changed', handleProjectChanged as EventListener);
    };
  }, []);

  // Sync with parent's showNewMemory prop
  useEffect(() => {
    if (showNewMemoryProp) {
      setShowNewMemory(true);
    }
  }, [showNewMemoryProp]);

  // Stats from API
  const [stats, setStats] = useState({
    memoryCount: 0,
    entityCount: 0,
    storageUsed: '0 MB',
    apiCalls: 0,
  });
  const [statsLoading, setStatsLoading] = useState(true);

  // Fetch real stats from API
  const fetchStats = useCallback(async () => {
    try {
      setStatsLoading(true);
      const analytics = await api.getAnalytics(currentProjectId);
      
      // Estimate storage based on memory count (avg ~2KB per memory)
      const estimatedKB = analytics.total_memories * 2;
      const storageStr = estimatedKB >= 1024 
        ? `${(estimatedKB / 1024).toFixed(1)} MB`
        : `${estimatedKB.toFixed(1)} KB`;
      
      setStats({
        memoryCount: analytics.total_memories,
        entityCount: analytics.total_entities,
        storageUsed: storageStr,
        apiCalls: analytics.stores_today + analytics.recalls_today,
      });
    } catch (err) {
      console.error('Failed to fetch stats:', err);
      // Fallback to local calculation if API fails
      if (memories.length > 0) {
        const uniqueEntities = new Set<string>();
        memories.forEach(m => m.entities?.forEach(e => uniqueEntities.add(e)));
        setStats({
          memoryCount: memories.length + (hasMore ? 100 : 0),
          entityCount: uniqueEntities.size,
          storageUsed: `${(memories.length * 0.5).toFixed(1)} KB`,
          apiCalls: 0,
        });
      }
    } finally {
      setStatsLoading(false);
    }
  }, [currentProjectId, memories, hasMore]);

  // Fetch stats on mount and when memories refresh
  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  useEffect(() => {
    setSelectedMemory(null);
    setIsSearching(false);
    clear();
  }, [currentProjectId, clear]);

  const handleSearch = (query: string) => {
    setIsSearching(true);
    search(query);
  };

  const handleClearSearch = () => {
    setIsSearching(false);
    clear();
  };

  const handleFocusSearch = () => {
    document.querySelector<HTMLInputElement>('[data-remembra-memory-search]')?.focus();
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
      <Suspense fallback={<SectionLoading label="Loading memory detail..." />}>
        <MemoryDetail
          memory={selectedMemory}
          onClose={handleCloseDetail}
          onDelete={() => {
            handleCloseDetail();
            refresh();
          }}
        />
      </Suspense>
    );
  }

  // New memory modal — rendered as overlay, not page replacement
  const newMemoryModal = showNewMemory ? (
    <Suspense fallback={null}>
      <StoreMemory
        projectId={currentProjectId}
        startOpen={true}
        onStored={() => {
          setShowNewMemory(false);
          onCloseNewMemory?.();
          refresh();
        }}
      />
    </Suspense>
  ) : null;

  const displayMemories = isSearching && results ? results.memories : memories;
  const displayLoading = isSearching ? searchLoading : loading;
  const displayError = isSearching ? searchError : error;

  const renderContent = () => {
    switch (activeTab) {
      case 'memories':
        return (
          <>
            <ControlPlaneOverview
              memoryCount={stats.memoryCount}
              entityCount={stats.entityCount}
              storageUsed={stats.storageUsed}
              apiCalls={stats.apiCalls}
              loading={statsLoading}
              wsConnected={wsConnected}
              currentProjectId={currentProjectId}
              onNewMemory={() => setShowNewMemory(true)}
              onSearch={handleFocusSearch}
              onOpenDebugger={() => onTabChange?.('debugger')}
              onOpenGraph={() => onTabChange?.('graph')}
            />

            {/* Stats Overview */}
            <StatsOverview
              memoryCount={stats.memoryCount}
              entityCount={stats.entityCount}
              storageUsed={stats.storageUsed}
              apiCalls={stats.apiCalls}
              loading={statsLoading}
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
                
                {/* WebSocket live status indicator */}
                <div
                  className={clsx(
                    'flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium',
                    wsConnected 
                      ? 'bg-green-500/10 text-green-500' 
                      : 'bg-yellow-500/10 text-yellow-500'
                  )}
                  title={wsConnected ? 'Live updates active' : 'Reconnecting...'}
                >
                  <span className={clsx(
                    'w-2 h-2 rounded-full',
                    wsConnected ? 'bg-green-500 animate-pulse' : 'bg-yellow-500'
                  )} />
                  {wsConnected ? 'Live' : 'Offline'}
                </div>
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
        return (
          <Suspense fallback={<SectionLoading label="Loading entity atlas..." />}>
            <EntityList projectId={currentProjectId} />
          </Suspense>
        );

      case 'graph':
        return (
          <Suspense fallback={<SectionLoading label="Loading knowledge graph..." />}>
            <EntityGraph projectId={currentProjectId} />
          </Suspense>
        );

      case 'decay':
        return (
          <Suspense fallback={<SectionLoading label="Loading decay report..." />}>
            <DecayReport projectId={currentProjectId} />
          </Suspense>
        );

      case 'debugger':
        return (
          <Suspense fallback={<SectionLoading label="Loading debugger..." />}>
            <QueryDebugger projectId={currentProjectId} />
          </Suspense>
        );

      case 'analytics':
        return (
          <Suspense fallback={<SectionLoading label="Loading analytics..." />}>
            <UsageAnalytics projectId={currentProjectId} />
          </Suspense>
        );

      case 'timeline':
        return (
          <Suspense fallback={<SectionLoading label="Loading timeline..." />}>
            <MemoryTimeline projectId={currentProjectId} />
          </Suspense>
        );

      case 'projects':
        return (
          <Suspense fallback={<SectionLoading label="Loading projects..." />}>
            <Projects />
          </Suspense>
        );

      case 'keys':
        return (
          <Suspense fallback={<SectionLoading label="Loading API keys..." />}>
            <ApiKeyManager />
          </Suspense>
        );

      case 'billing':
        return (
          <Suspense fallback={<SectionLoading label="Loading billing..." />}>
            <Billing />
          </Suspense>
        );

      case 'teams':
        return (
          <Suspense fallback={<SectionLoading label="Loading teams..." />}>
            <Teams />
          </Suspense>
        );

      case 'settings':
        return (
          <Suspense fallback={<SectionLoading label="Loading settings..." />}>
            <Settings onLogout={onLogout || (() => {})} />
          </Suspense>
        );

      case 'admin':
        return (
          <Suspense fallback={<SectionLoading label="Loading admin..." />}>
            <Admin />
          </Suspense>
        );

      default:
        return null;
    }
  };

  return (
    <div className="min-h-full">
      <AnimatePresence mode="wait">
        <motion.div
          key={activeTab}
          variants={pageTransition}
          initial="initial"
          animate="animate"
          exit="exit"
        >
          {renderContent()}
        </motion.div>
      </AnimatePresence>
      {newMemoryModal}
    </div>
  );
}
