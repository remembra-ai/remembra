// The dashed trail across the column heads, one station per column. When a
// task changes column on the live stream, an orange pixel packet runs from
// the old station to the new one (≤900 ms; with reduced motion it lands without travelling). The
// station a packet arrives at stays lit for a moment.

import type { CSSProperties } from 'react';
import type { ColumnId } from './model';
import { COLUMN_IDS } from './model';

export interface Packet {
  id: string;
  from: ColumnId | null;
  to: ColumnId;
}

function center(col: ColumnId): number {
  return ((COLUMN_IDS.indexOf(col) + 0.5) / COLUMN_IDS.length) * 100;
}

export function PacketTrail({ packets, onDone }: { packets: Packet[]; onDone: (id: string) => void }) {
  const hot = new Set(packets.map((p) => p.to));
  return (
    <div className="cb-trail" aria-hidden="true">
      {COLUMN_IDS.map((c) => (
        <span key={c} className="cb-station" data-hot={hot.has(c)} style={{ left: `${center(c)}%` }} />
      ))}
      {packets.map((p) => {
        const to = center(p.to);
        const from = p.from ? center(p.from) : Math.max(0, to - 100 / COLUMN_IDS.length);
        const dir = to >= from ? 1 : -1;
        const dur = 450 + Math.abs(to - from) * 9;
        return (
          <span
            key={p.id}
            className="cb-packet"
            data-dir={dir}
            onAnimationEnd={() => onDone(p.id)}
            style={
              {
                '--cb-from': `${from}%`,
                '--cb-to': `${to}%`,
                '--cb-dur': `${Math.min(900, dur)}ms`,
                left: `${to}%`,
              } as CSSProperties
            }
          />
        );
      })}
    </div>
  );
}
