import { useState } from 'react';
import { useMemories, useSearch } from '../hooks/useMemories';
import { SearchBar } from '../components/SearchBar';
import { MemoryList } from '../components/MemoryList';
import { MemoryDetail } from './MemoryDetail';
import { DecayReport } from '../components/DecayReport';
import { StoreMemory } from '../components/StoreMemory';
import { EntityList } from '../components/EntityList';
import { EntityGraph } from '../components/EntityGraph';
import type { Memory } from '../lib/api';
import { RefreshCw, Database, TrendingDown, Users, Share2 } from 'lucide-react';
import clsx from 'clsx';

type TabType = 'memories' | 'entities' | 'graph' | 'decay';

export function Dashboard() {
  const { memories, loading, error, hasMore, refresh, loadMore } = useMemories();
  const { results, loading: searchLoading, error: searchError, search, clear } = useSearch();
  const [selectedMemory, setSelectedMemory] = useState<Memory | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [activeTab, setActiveTab] = useState<TabType>('memories');

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

  const displayMemories = isSearching && results ? results.memories : memories;
  const displayLoading = isSearching ? searchLoading : loading;
  const displayError = isSearching ? searchError : error;

  return (
    <div className="max-w-4xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
      {/* Tabs */}
      <div className="border-b border-gray-200 dark:border-gray-700 mb-6">
        <nav className="flex space-x-8">
          <button
            onClick={() => setActiveTab('memories')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'memories'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <Database className="w-4 h-4" />
            Memories
          </button>
          <button
            onClick={() => setActiveTab('entities')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'entities'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <Users className="w-4 h-4" />
            Entities
          </button>
          <button
            onClick={() => setActiveTab('graph')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'graph'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <Share2 className="w-4 h-4" />
            Graph
          </button>
          <button
            onClick={() => setActiveTab('decay')}
            className={clsx(
              'py-4 px-1 border-b-2 font-medium text-sm flex items-center gap-2 transition-colors',
              activeTab === 'decay'
                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-300'
            )}
          >
            <TrendingDown className="w-4 h-4" />
            Decay Report
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'memories' && (
        <>
          {/* Search */}
          <div className="mb-6">
            <SearchBar
              onSearch={handleSearch}
              onClear={handleClearSearch}
              loading={searchLoading}
            />
          </div>

          {/* Header */}
          <div className="flex items-center justify-between mb-4">
            <div>
              {isSearching && results ? (
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                  {results.memories.length} result{results.memories.length !== 1 ? 's' : ''} for "{results.query}"
                </h2>
              ) : (
                <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                  All Memories
                  {memories.length > 0 && (
                    <span className="ml-2 text-sm font-normal text-gray-500">
                      ({memories.length}{hasMore ? '+' : ''})
                    </span>
                  )}
                </h2>
              )}
            </div>

            {!isSearching && (
              <button
                onClick={refresh}
                disabled={loading}
                className={clsx(
                  'p-2 rounded-lg text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200',
                  'hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors',
                  loading && 'opacity-50 cursor-not-allowed'
                )}
                title="Refresh"
              >
                <RefreshCw className={clsx('w-5 h-5', loading && 'animate-spin')} />
              </button>
            )}
          </div>

          {/* Context (for search results) */}
          {isSearching && results?.context && (
            <div className="mb-6 p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-100 dark:border-blue-800">
              <h3 className="text-sm font-medium text-blue-800 dark:text-blue-300 mb-2">
                Context Summary
              </h3>
              <p className="text-sm text-blue-700 dark:text-blue-400 whitespace-pre-wrap">
                {results.context}
              </p>
            </div>
          )}

          {/* Memory List */}
          <MemoryList
            memories={displayMemories}
            loading={displayLoading}
            error={displayError}
            hasMore={!isSearching && hasMore}
            onLoadMore={!isSearching ? loadMore : undefined}
            onSelectMemory={handleSelectMemory}
            showRelevance={isSearching}
            emptyMessage={isSearching ? 'No memories match your search' : 'No memories yet. Start by storing some!'}
          />
        </>
      )}

      {activeTab === 'entities' && (
        <EntityList projectId="default" />
      )}

      {activeTab === 'graph' && (
        <EntityGraph projectId="default" />
      )}

      {activeTab === 'decay' && (
        <DecayReport projectId="default" />
      )}

      {/* Floating Add Memory Button */}
      <StoreMemory onStored={refresh} projectId="default" />
    </div>
  );
}
