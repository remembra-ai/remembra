// Report details for the cards that show a seal (Done) or a review action
// (Review). Fetched per task (`GET /tasks/{id}/reports`), at most four at a
// time, and cached by report id plus task version, so an accepted review or a
// new report refetches while unchanged cards never do.

import { useEffect, useRef, useState } from 'react';
import type { CrewApi } from '../../../lib/crew/api';
import { loadTaskReports } from './actions';
import type { ReportDetail } from './model';

export interface ReportWant {
  taskId: string;
  reportId: string;
  version: number;
}

const MAX_PARALLEL = 4;

export function reportKey(w: Pick<ReportWant, 'reportId' | 'version'>): string {
  return `${w.reportId}@${w.version}`;
}

export function useReportDetails(api: CrewApi, wanted: readonly ReportWant[]): Record<string, ReportDetail> {
  const [cache, setCache] = useState<Record<string, ReportDetail>>({});
  const known = useRef(new Set<string>());
  const wantedKey = wanted.map(reportKey).join(',');
  const wantedRef = useRef(wanted);
  useEffect(() => {
    wantedRef.current = wanted;
  });

  useEffect(() => {
    let cancelled = false;
    const seen = known.current;
    const queue = wantedRef.current.filter((w) => !seen.has(reportKey(w)));
    for (const w of queue) seen.add(reportKey(w));
    const inflight = new Set<string>();
    const next = () => {
      while (!cancelled && inflight.size < MAX_PARALLEL && queue.length) {
        const w = queue.shift()!;
        const key = reportKey(w);
        inflight.add(key);
        loadTaskReports(api, w.taskId)
          .then((reports) => {
            if (cancelled) return;
            const found = reports.find((r) => r.id === w.reportId);
            if (found) setCache((c) => ({ ...c, [key]: found }));
          })
          .catch(() => {
            seen.delete(key); // retried on the next change
          })
          .finally(() => {
            inflight.delete(key);
            next();
          });
      }
    };
    next();
    return () => {
      cancelled = true;
      // unfinished work is forgotten so the next run asks again
      for (const w of queue) seen.delete(reportKey(w));
      for (const key of inflight) seen.delete(key);
    };
  }, [api, wantedKey]);

  return cache;
}
