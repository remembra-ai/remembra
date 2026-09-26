// Live movement on the board, read straight off the crew store (outside
// render): which tasks just changed column and when, the packets to run
// along the column trail, the ember burst for the header cloud, and when each
// live checkpoint was first seen (the reducer keeps only the latest per
// session, without a timestamp).

import { useCallback, useEffect, useRef, useState } from 'react';
import { useCrewRuntime } from '../../../lib/crew/context';
import type { CheckpointView, TaskView } from '../../../lib/crew/types';
import type { CloudBurst } from './DitherCloud';
import { COLUMN_IDS, statusMoves, type ColumnId, type StatusMove } from './model';
import type { Packet } from './PacketTrail';

export interface TimedMove extends StatusMove {
  at: string;
  key: string;
}

export interface LiveMoves {
  moves: TimedMove[];
  /** task id → when it last changed column (ISO), from this page's point of view. */
  since: Record<string, string>;
  packets: Packet[];
  burst: CloudBurst | null;
  checkpointSeen: Record<string, string>;
  packetDone: (id: string) => void;
}

interface Inner {
  moves: TimedMove[];
  since: Record<string, string>;
  packets: Packet[];
  burst: CloudBurst | null;
  checkpointSeen: Record<string, string>;
}

const EMPTY: Inner = { moves: [], since: {}, packets: [], burst: null, checkpointSeen: {} };

export function useLiveMoves(crewId: string | null): LiveMoves {
  const runtime = useCrewRuntime();
  const [inner, setInner] = useState<Inner>(EMPTY);
  const prevTasks = useRef<Record<string, TaskView> | null>(null);
  const prevCheckpoints = useRef<Record<string, CheckpointView> | null>(null);

  useEffect(() => {
    if (!crewId) return undefined;
    const store = runtime.storeFor(crewId);
    prevTasks.current = store.getView().state?.tasks ?? null;
    prevCheckpoints.current = store.getView().state?.checkpoints ?? null;
    const onChange = () => {
      const state = store.getView().state;
      if (!state) return;
      const at = new Date().toISOString();
      const tasks = state.tasks;
      const before = prevTasks.current;
      prevTasks.current = tasks;
      const moves = before && before !== tasks ? statusMoves(before, tasks) : [];
      const ckps = state.checkpoints;
      const ckpBefore = prevCheckpoints.current;
      prevCheckpoints.current = ckps;
      const newCkps = ckpBefore && ckpBefore !== ckps ? Object.values(ckps).filter((c) => !Object.values(ckpBefore).some((o) => o.id === c.id)) : [];
      if (!moves.length && !newCkps.length) return;
      setInner((cur) => {
        const next: Inner = { ...cur };
        if (newCkps.length) next.checkpointSeen = { ...cur.checkpointSeen, ...Object.fromEntries(newCkps.map((c) => [c.id, at])) };
        if (moves.length) {
          const stamped = moves.map((m) => ({ ...m, at, key: `${m.taskId}:${tasks[m.taskId]?.version ?? 0}:${at}` }));
          next.moves = [...cur.moves, ...stamped].slice(-12);
          next.since = { ...cur.since, ...Object.fromEntries(stamped.map((m) => [m.taskId, at])) };
          const shown = stamped.filter((m): m is TimedMove & { to: ColumnId } => m.to !== null);
          if (shown.length) {
            next.packets = [...cur.packets, ...shown.map((m) => ({ id: m.key, from: m.from, to: m.to }))].slice(-4);
            const last = shown[shown.length - 1];
            next.burst = { id: last.key, x: (COLUMN_IDS.indexOf(last.to) + 0.5) / COLUMN_IDS.length };
          }
        }
        return next;
      });
    };
    return store.subscribe(onChange);
  }, [runtime, crewId]);

  const packetDone = useCallback((id: string) => setInner((cur) => ({ ...cur, packets: cur.packets.filter((p) => p.id !== id) })), []);
  return { ...inner, packetDone };
}
