// Words for the live strip (status never depends on colour alone).

import type { CrewStreamStatus } from '../../../lib/crew/store';
import type { ConnectionStatus } from '../../../lib/crew/socket';
import type { CrewEvent } from '../../../lib/crew/types';

export function liveWords(status: CrewStreamStatus, connection: ConnectionStatus): { text: string; live: boolean } {
  if (status === 'live') return { text: 'live', live: true };
  if (status === 'resyncing') return { text: 'catching up', live: false };
  if (connection === 'unauthorized') return { text: 'signed out', live: false };
  if (status === 'polling') return { text: 'updating every few seconds', live: false };
  if (status === 'loading') return { text: 'connecting', live: false };
  if (status === 'error') return { text: 'reconnecting', live: false };
  return { text: status.replace('_', ' '), live: false };
}

export function recentCount(events: readonly CrewEvent[], nowMs: number, windowMs = 10 * 60 * 1000): number {
  let n = 0;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const at = Date.parse(events[i].ts);
    if (!Number.isFinite(at)) continue;
    if (nowMs - at > windowMs) break;
    n += 1;
  }
  return n;
}
