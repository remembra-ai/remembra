import { useState, useEffect, useCallback, useRef } from 'react';
import { api } from '../lib/api';
import type { Memory, RecallResult } from '../lib/api';
import { useWebSocket, type WebSocketEvent } from './useWebSocket';

export function useMemories(limit = 20, projectId?: string) {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [wsConnected, setWsConnected] = useState(false);
  const lastRefreshRef = useRef<number>(0);

  const fetchMemories = useCallback(async (pageOffset: number, reset = false) => {
    try {
      setLoading(true);
      setError(null);
      const result = await api.listMemories({ limit, offset: pageOffset, project_id: projectId });
      
      if (reset) {
        setMemories(result);
        setOffset(result.length);
      } else {
        setMemories(prev => [...prev, ...result]);
        setOffset(prev => prev + result.length);
      }
      
      setHasMore(result.length === limit);
      lastRefreshRef.current = Date.now();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch memories');
    } finally {
      setLoading(false);
    }
  }, [limit, projectId]);

  const refresh = useCallback(() => {
    setMemories([]);
    setOffset(0);
    setHasMore(true);
    fetchMemories(0, true);
  }, [fetchMemories]);

  const loadMore = useCallback(() => {
    if (!loading && hasMore) {
      fetchMemories(offset, false);
    }
  }, [fetchMemories, hasMore, loading, offset]);

  // Handle WebSocket events for real-time updates
  const handleMemoryEvent = useCallback((event: WebSocketEvent) => {
    // Debounce rapid events - don't refresh more than once per second
    const now = Date.now();
    if (now - lastRefreshRef.current < 1000) {
      return;
    }

    console.log('[WebSocket] Memory event received:', event.type, event.data);
    
    // Refresh the list on any memory change
    if (event.type === 'memory.created' || event.type === 'memory.updated' || event.type === 'memory.deleted') {
      refresh();
    }
  }, [refresh]);

  // WebSocket connection for real-time updates
  // Pass either API key or JWT token for authentication
  const authToken = api.getApiKey() || api.getJwtToken() || undefined;
  const { connected } = useWebSocket({
    namespace: projectId || 'default',
    projectId,
    apiKey: api.getApiKey() || undefined,
    token: api.getJwtToken() || undefined,
    onMemoryEvent: handleMemoryEvent,
    onConnectionChange: setWsConnected,
    autoReconnect: true,
  });

  useEffect(() => {
    if (api.isAuthenticated()) {
      setMemories([]);
      setOffset(0);
      setHasMore(true);
      void fetchMemories(0, true);
    }
  }, [fetchMemories]);

  return { memories, loading, error, hasMore, refresh, loadMore, wsConnected: connected };
}

export function useSearch(projectId?: string) {
  const [results, setResults] = useState<RecallResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(async (query: string) => {
    if (!query.trim()) {
      setResults(null);
      setError(null);
      return;
    }

    try {
      setLoading(true);
      setError(null);
      const result = await api.recallMemories({ query, limit: 10, project_id: projectId });
      setResults(result);
    } catch (err) {
      // Handle different error types
      if (err instanceof Error) {
        setError(err.message);
      } else if (typeof err === 'object' && err !== null) {
        setError(JSON.stringify(err));
      } else {
        setError('Search failed');
      }
      setResults(null);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  const clear = useCallback(() => {
    setResults(null);
    setError(null);
  }, []);

  return { results, loading, error, search, clear };
}

export function useMemory(id: string | null) {
  const [memory, setMemory] = useState<Memory | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) {
      setMemory(null);
      return;
    }

    const fetchMemory = async () => {
      try {
        setLoading(true);
        setError(null);
        const result = await api.getMemory(id);
        setMemory(result);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to fetch memory');
      } finally {
        setLoading(false);
      }
    };

    fetchMemory();
  }, [id]);

  return { memory, loading, error };
}
