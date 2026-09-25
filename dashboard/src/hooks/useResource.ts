import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../lib/api';

/**
 * Failures that polling cannot fix: signed out, no permission, the feature is
 * missing or turned off on this server. Polling stops until a manual refresh.
 */
export function isPermanentFailure(error: unknown): boolean {
  return error instanceof ApiError && [401, 403, 404, 405, 501, 503].includes(error.status);
}

interface ResourceState<T> {
  key: string | null;
  data: T | undefined;
  error: unknown;
  updatedAt: number;
}

export interface Resource<T> {
  /** Data for the current key (undefined until its first successful load). */
  data: T | undefined;
  /** The last error for the current key; cleared by the next success. */
  error: unknown;
  /** True until the first response (data or error) for the current key. */
  loading: boolean;
  /** True while a manual refresh is in flight. */
  refreshing: boolean;
  /** When the data was last loaded (ms since epoch), 0 if never. */
  updatedAt: number;
  refresh: () => void;
}

/**
 * Load data for `key` (null = don't load) and optionally poll it.
 *
 * Polling pauses while the tab is hidden and catches up as soon as it is
 * visible again. A failed poll keeps the last good data and reports the error;
 * a permanent failure (see isPermanentFailure) stops polling until refresh().
 * Responses for a key that is no longer current are dropped.
 */
export function useResource<T>(
  key: string | null,
  loader: () => Promise<T>,
  options: { pollMs?: number } = {},
): Resource<T> {
  const { pollMs } = options;
  const loaderRef = useRef(loader);
  const [state, setState] = useState<ResourceState<T>>({ key: null, data: undefined, error: null, updatedAt: 0 });
  const [nonce, setNonce] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    loaderRef.current = loader;
  });

  useEffect(() => {
    if (key === null) return undefined;
    let cancelled = false;
    let inFlight = false;
    let stopped = false;
    let timer: number | undefined;

    const run = () => {
      if (inFlight) return;
      inFlight = true;
      loaderRef.current().then(
        (data) => {
          inFlight = false;
          if (cancelled) return;
          setState({ key, data, error: null, updatedAt: Date.now() });
          setRefreshing(false);
        },
        (error: unknown) => {
          inFlight = false;
          if (cancelled) return;
          if (isPermanentFailure(error)) {
            stopped = true;
            if (timer !== undefined) window.clearInterval(timer);
          }
          setState((prev) => ({
            key,
            data: prev.key === key ? prev.data : undefined,
            error,
            updatedAt: prev.key === key ? prev.updatedAt : 0,
          }));
          setRefreshing(false);
        },
      );
    };

    run();

    const onVisible = () => {
      if (document.visibilityState === 'visible' && !stopped) run();
    };
    if (pollMs) {
      timer = window.setInterval(() => {
        if (document.visibilityState === 'visible' && !stopped) run();
      }, pollMs);
      document.addEventListener('visibilitychange', onVisible);
    }
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [key, pollMs, nonce]);

  const refresh = useCallback(() => {
    setRefreshing(true);
    setNonce((n) => n + 1);
  }, []);

  const current = state.key === key;
  return {
    data: current ? state.data : undefined,
    error: current ? state.error : null,
    loading: key !== null && !current,
    refreshing,
    updatedAt: current ? state.updatedAt : 0,
    refresh,
  };
}

/** A clock that re-renders every `intervalMs` so relative times stay fresh. */
export function useNow(intervalMs = 30000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}
