/**
 * WebSocket hook for real-time memory updates.
 * 
 * Connects to the Remembra WebSocket endpoint and provides
 * real-time updates for memory create/update/delete events.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

export interface WebSocketEvent {
  type: string;
  data: Record<string, unknown>;
  timestamp: string;
  namespace: string;
  project_id?: string;
}

export interface UseWebSocketOptions {
  /** API URL (defaults to current origin) */
  baseUrl?: string;
  /** Namespace to subscribe to */
  namespace?: string;
  /** Project ID filter */
  projectId?: string;
  /** API key for authentication */
  apiKey?: string;
  /** JWT token for authentication (alternative to apiKey) */
  token?: string;
  /** Auto-reconnect on disconnect */
  autoReconnect?: boolean;
  /** Reconnect delay in ms */
  reconnectDelay?: number;
  /** Callback when a memory event is received */
  onMemoryEvent?: (event: WebSocketEvent) => void;
  /** Callback when connection state changes */
  onConnectionChange?: (connected: boolean) => void;
}

export interface UseWebSocketReturn {
  /** Whether the WebSocket is connected */
  connected: boolean;
  /** Last received event */
  lastEvent: WebSocketEvent | null;
  /** Connection error, if any */
  error: string | null;
  /** Manually reconnect */
  reconnect: () => void;
  /** Disconnect */
  disconnect: () => void;
  /** Change subscription */
  subscribe: (namespace: string, projectId?: string) => void;
}

export function useWebSocket(options: UseWebSocketOptions = {}): UseWebSocketReturn {
  const {
    baseUrl,
    namespace = 'default',
    projectId,
    apiKey,
    token,
    autoReconnect = true,
    reconnectDelay = 3000,
    onMemoryEvent,
    onConnectionChange,
  } = options;

  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<WebSocketEvent | null>(null);
  const [error, setError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const currentNamespaceRef = useRef(namespace);
  const currentProjectIdRef = useRef(projectId);

  const getWebSocketUrl = useCallback(() => {
    // Determine the base URL
    let base = baseUrl;
    if (!base) {
      // Use current origin, converting http to ws and https to wss
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      base = `${protocol}//${window.location.host}`;
    } else {
      // Convert http/https to ws/wss
      base = base.replace(/^http/, 'ws');
    }

    // Build query params
    const params = new URLSearchParams();
    params.set('namespace', currentNamespaceRef.current);
    if (currentProjectIdRef.current) {
      params.set('project_id', currentProjectIdRef.current);
    }
    if (apiKey) {
      params.set('api_key', apiKey);
    }
    if (token) {
      params.set('token', token);
    }

    return `${base}/ws?${params.toString()}`;
  }, [baseUrl, apiKey, token]);

  const connect = useCallback(() => {
    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.close();
    }

    // Clear any pending reconnect
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    try {
      const url = getWebSocketUrl();
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setError(null);
        onConnectionChange?.(true);
        console.log('[WebSocket] Connected to', url);
      };

      ws.onclose = (event) => {
        setConnected(false);
        onConnectionChange?.(false);
        console.log('[WebSocket] Disconnected:', event.code, event.reason);

        // Auto-reconnect if enabled and not a clean close
        if (autoReconnect && event.code !== 1000) {
          reconnectTimeoutRef.current = setTimeout(() => {
            console.log('[WebSocket] Attempting reconnect...');
            connect();
          }, reconnectDelay);
        }
      };

      ws.onerror = (event) => {
        console.error('[WebSocket] Error:', event);
        setError('WebSocket connection error');
      };

      ws.onmessage = (event) => {
        try {
          // Handle ping/pong
          if (event.data === 'ping') {
            ws.send('pong');
            return;
          }
          if (event.data === 'pong') {
            return;
          }

          const data = JSON.parse(event.data) as WebSocketEvent;
          setLastEvent(data);

          // Call callback for memory events
          if (data.type.startsWith('memory.')) {
            onMemoryEvent?.(data);
          }
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e);
        }
      };
    } catch (e) {
      setError(`Failed to connect: ${e}`);
    }
  }, [getWebSocketUrl, autoReconnect, reconnectDelay, onMemoryEvent, onConnectionChange]);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close(1000, 'User disconnected');
      wsRef.current = null;
    }
    setConnected(false);
  }, []);

  const reconnect = useCallback(() => {
    disconnect();
    connect();
  }, [disconnect, connect]);

  const subscribe = useCallback((newNamespace: string, newProjectId?: string) => {
    currentNamespaceRef.current = newNamespace;
    currentProjectIdRef.current = newProjectId;

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      // Send subscription change message
      wsRef.current.send(JSON.stringify({
        type: 'subscribe',
        namespace: newNamespace,
        project_id: newProjectId,
      }));
    } else {
      // Reconnect with new params
      connect();
    }
  }, [connect]);

  // Connect on mount
  useEffect(() => {
    connect();
    return () => {
      disconnect();
    };
  }, [connect, disconnect]);

  // Update refs when props change
  useEffect(() => {
    currentNamespaceRef.current = namespace;
    currentProjectIdRef.current = projectId;
  }, [namespace, projectId]);

  return {
    connected,
    lastEvent,
    error,
    reconnect,
    disconnect,
    subscribe,
  };
}

export default useWebSocket;
