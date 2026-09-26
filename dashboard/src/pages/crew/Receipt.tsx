// Report receipt screen: `#/crew?project=X&view=report&report=…[&task=…]` (§9.1, §9.10).
// The task id in the link makes an older (superseded) report resolvable; a
// link with only the report id (e.g. from an email) is resolved from what
// the live crew state knows, then by searching the crew's tasks.

import '../../components/crew/board/board.css';
import { useState } from 'react';
import { useNow, useResource } from '../../hooks/useResource';
import { useCrewSocket } from '../../hooks/useCrewSocket';
import { crewApi } from '../../lib/crew/api';
import type { CrewDetail } from '../../lib/crew/types';
import { CompletionReport } from '../../components/crew/board/CompletionReport';
import { ReportReceipt } from '../../components/crew/board/ReportReceipt';
import { ReceiptNotFound, loadReceipt, type Receipt as ReceiptData } from '../../components/crew/board/actions';
import { boardHref, receiptHref, taskForReport } from '../../components/crew/board/model';
import { Card, ErrorNotice, TrailSkeleton } from '../../components/relay/ui';

export function Receipt({ crewId, project, reportId, taskParam }: { crewId: string; project: string; reportId: string; taskParam: string | null }) {
  const crew = useCrewSocket(crewId);
  const now = useNow(30_000);
  const detail = useResource<CrewDetail>(`crew-detail:${crewId}`, () => crewApi.getCrew(crewId));
  const human = !!detail.data?.human && (detail.data.permissions ?? []).includes('crew:override');
  const hint = taskParam ?? taskForReport(reportId, crew.state, Object.values(crew.state?.tasks ?? {}));
  // Reload when the task changes on the live stream (a review decided, a new report).
  const version = hint ? (crew.state?.tasks[hint]?.version ?? 0) : 0;
  const receipt = useResource<ReceiptData>(`receipt:${crewId}:${reportId}:${hint ?? ''}:${version}`, () => loadReceipt(crewApi, crewId, reportId, hint));
  const [reviewing, setReviewing] = useState(false);

  if (receipt.loading && !receipt.data) return <TrailSkeleton rows={4} />;
  if (receipt.error && !receipt.data) {
    if (receipt.error instanceof ReceiptNotFound) {
      return (
        <Card className="p-4 sm:p-5">
          <p className="rr-eyebrow">Receipt not found</p>
          <p className="mt-2 text-sm text-ink-2">
            Report <span className="font-mono">{reportId}</span> is not on any task of this crew you can see.{' '}
            <a href={boardHref(project)} className="font-semibold text-signal-ink hover:underline">
              Open the task board
            </a>
            .
          </p>
        </Card>
      );
    }
    return <ErrorNotice error={receipt.error} what="this receipt" onRetry={receipt.refresh} />;
  }
  const data = receipt.data!;
  return (
    <>
      <ReportReceipt receipt={data} project={project} state={crew.state} now={now} human={human} onReview={() => setReviewing(true)} />
      {reviewing && (
        <CompletionReport
          api={crewApi}
          task={data.task}
          receiptHref={(rid) => receiptHref(project, rid, data.task.id)}
          onClose={() => setReviewing(false)}
          onChanged={() => receipt.refresh()}
        />
      )}
    </>
  );
}
