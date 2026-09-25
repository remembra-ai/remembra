import { createContext, useContext } from 'react';
import type { Resource } from './useResource';
import type { UsageResponse } from '../lib/api';
import type { ActivitySummary, InboxSummary, TrailResponse } from '../lib/relay';

/** Relay data shared by Home, Agents and the navigation badges (polled every 30s). */
export interface RelayData {
  /** The newest trail entries across every project. */
  trail: Resource<TrailResponse>;
  summary: Resource<ActivitySummary>;
  inbox: Resource<InboxSummary>;
  /** Plan usage; errors mean the server has no usage metering (the meter hides). */
  usage: Resource<UsageResponse>;
  refreshAll: () => void;
}

export const RelayDataContext = createContext<RelayData | null>(null);

export function useRelayData(): RelayData {
  const value = useContext(RelayDataContext);
  if (!value) throw new Error('useRelayData must be used inside <RelayDataProvider>');
  return value;
}

export const RELAY_POLL_MS = 30000;
export const TRAIL_HEAD_LIMIT = 30;
