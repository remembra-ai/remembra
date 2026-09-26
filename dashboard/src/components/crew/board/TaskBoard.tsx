// The Task Board (§9.7). Columns Up next · In progress · Blocked · Review ·
// Done, plus Stalled; swimlanes by Phase, Agent or Zone. Every task of the
// crew comes from REST (Done included), kept live by the crew stream: a card
// moves the moment its event lands, an orange packet runs along the column
// trail, and an ember ring blooms in the header cloud over its new column.
//
// No report means no Done: dropping a card on Done opens the report or
// waiver flow; a stalled card offers "Pick up baton"; everything else an
// agent does itself. The card menu does all of it without a mouse.

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type DragEvent } from 'react';
import clsx from 'clsx';
import { Plus, Rows3 } from 'lucide-react';
import { toast } from 'sonner';
import { useNow, useResource } from '../../../hooks/useResource';
import type { UseCrewSocket } from '../../../hooks/useCrewSocket';
import type { CrewApi } from '../../../lib/crew/api';
import { navigate } from '../../../lib/nav';
import type { CrewDetail } from '../../../lib/crew/types';
import { ErrorNotice, StaleNotice, TrailSkeleton } from '../../relay/ui';
import { explainActionError, loadBoard, reopenTask, type BoardData } from './actions';
import { CardMenu, type MenuItem } from './CardMenu';
import { CompletionReport } from './CompletionReport';
import { DependencyLines } from './DependencyLines';
import { DitherCloud } from './DitherCloud';
import {
  BOARD_COLUMNS,
  COLUMN_IDS,
  SWIMLANES,
  STATUS_TEXT,
  acceptanceMeter,
  ageText,
  buildLanes,
  checkpointDots,
  columnCounts,
  columnOf,
  columnTitle,
  dependencyEdges,
  dependencyRefs,
  dropIntent,
  dropTargets,
  mergeCheckpoints,
  mergeTasks,
  ownerOf,
  receiptHref,
  resolveTaskParam,
  restIsStale,
  savedWork,
  sealItems,
  sealLine,
  statusSince,
  zoneChips,
  type ColumnId,
  type StatusMove,
  type SwimlaneBy,
  type TaskDetail,
} from './model';
import { NewTask } from './NewTask';
import { PacketTrail } from './PacketTrail';
import { PickUpBaton } from './PickUpBaton';
import { TaskCard, type CardHandlers, type CardModel } from './TaskCard';
import { TaskDrawer } from './TaskDrawer';
import { useLiveMoves } from './useLiveMoves';
import { reportKey, useReportDetails, type ReportWant } from './useReportDetails';

const DONE_VISIBLE = 8;
const PHONE_QUERY = '(max-width: 767px)';

function subscribePhone(onChange: () => void): () => void {
  const mq = window.matchMedia?.(PHONE_QUERY);
  mq?.addEventListener('change', onChange);
  return () => mq?.removeEventListener('change', onChange);
}

function useIsPhone(): boolean {
  return useSyncExternalStore(subscribePhone, () => !!window.matchMedia?.(PHONE_QUERY).matches, () => false);
}

type Flow = { kind: 'report' | 'pickup'; taskId: string } | { kind: 'new' } | null;

function StatusStrip({
  live,
  statusText,
  counts,
  last,
}: {
  live: boolean;
  statusText: string;
  counts: Record<ColumnId, number>;
  last: (StatusMove & { at: string }) | null;
}) {
  const now = useNow(5000);
  const lastText = last
    ? `T-${last.number} → ${last.to ? columnTitle(last.to).toLowerCase() : STATUS_TEXT[last.toStatus]} ${ageText(last.at, now) ?? ''} ago`
    : null;
  return (
    <p className="cb-strip relative flex max-w-full items-center gap-2.5 px-3 py-1.5 text-[12px] text-ink-2">
      <span className={clsx('cb-strip-dot', live && 'rr-pulse')} data-live={live} aria-hidden="true" />
      <span className="shrink-0 font-semibold text-ink">{statusText}</span>
      <span className="truncate">
        {counts.progress} in progress · {counts.review} to review · {counts.stalled} stalled · {counts.done} done
        {lastText && <span className="text-ink"> · {lastText}</span>}
      </span>
    </p>
  );
}

export function TaskBoard({
  api,
  crewId,
  project,
  crew,
  taskParam,
  lanes: lanesParam,
}: {
  api: CrewApi;
  crewId: string;
  project: string;
  crew: UseCrewSocket;
  taskParam: string | null;
  lanes: SwimlaneBy;
}) {
  const state = crew.state;
  const detail = useResource<CrewDetail>(`crew-detail:${crewId}`, () => api.getCrew(crewId));
  const board = useResource<BoardData>(`board:${crewId}`, () => loadBoard(api, crewId), { pollMs: 60_000 });
  const human = !!detail.data?.human && (detail.data.permissions ?? []).includes('crew:override');
  const now = useNow(30_000);

  // -- live movement (read off the store: which tasks just moved, packets, the cloud burst)
  const live = useLiveMoves(crewId);
  const liveTasks = useMemo(() => state?.tasks ?? {}, [state?.tasks]);

  // -- data --------------------------------------------------------------------------------
  const tasks = useMemo(() => mergeTasks(board.data?.tasks, liveTasks), [board.data?.tasks, liveTasks]);
  const refreshBoard = board.refresh;
  // A live task newer than the REST list: refetch it once for that set of versions (timestamps, waivers).
  const staleKey = restIsStale(board.data?.tasks, liveTasks)
    ? Object.values(liveTasks)
        .map((t) => `${t.id}:${t.version}`)
        .sort()
        .join(',')
    : '';
  const refreshedFor = useRef('');
  useEffect(() => {
    if (!staleKey || refreshedFor.current === staleKey) return undefined;
    const timer = setTimeout(() => {
      refreshedFor.current = staleKey;
      refreshBoard();
    }, 250);
    return () => clearTimeout(timer);
  }, [staleKey, refreshBoard]);
  const checkpoints = useMemo(
    () => mergeCheckpoints(board.data?.checkpoints, state?.checkpoints ?? {}, live.checkpointSeen),
    [board.data?.checkpoints, state?.checkpoints, live.checkpointSeen],
  );
  const byId = useMemo(() => new Map(tasks.map((t) => [t.id, t])), [tasks]);

  const wanted = useMemo<ReportWant[]>(() => {
    const done = tasks
      .filter((t) => t.current_report_id && (t.status === 'done' || t.status === 'review' || t.status === 'cancelled'))
      .sort((a, b) => (b.done_at ?? b.updated_at ?? '').localeCompare(a.done_at ?? a.updated_at ?? ''))
      .slice(0, 40);
    return done.map((t) => ({ taskId: t.id, reportId: t.current_report_id as string, version: t.version }));
  }, [tasks]);
  const reports = useReportDetails(api, wanted);

  const [showCancelled, setShowCancelled] = useState(false);
  const lanes = useMemo(() => buildLanes(tasks, lanesParam, state, { showCancelled }), [tasks, lanesParam, state, showCancelled]);
  const counts = useMemo(() => columnCounts(tasks, showCancelled), [tasks, showCancelled]);
  const edges = useMemo(() => dependencyEdges(tasks.filter((t) => columnOf(t.status, showCancelled))), [tasks, showCancelled]);

  const cards = useMemo(() => {
    const out = new Map<string, CardModel>();
    for (const t of tasks) {
      const column = columnOf(t.status, true);
      if (!column) continue;
      const liveReport = state?.reports[t.id];
      const detailReport = t.current_report_id ? reports[reportKey({ reportId: t.current_report_id, version: t.version })] : undefined;
      const report = detailReport ?? (liveReport && liveReport.id === t.current_report_id ? liveReport : undefined);
      const since = statusSince(t, live.since);
      const showSeal = (column === 'done' || t.status === 'cancelled') && report;
      out.set(t.id, {
        id: t.id,
        number: t.number,
        title: t.title,
        column,
        cancelled: t.status === 'cancelled',
        statusText: STATUS_TEXT[t.status],
        owner: ownerOf(t, state),
        zones: zoneChips(t, state),
        meter: acceptanceMeter(t, report ?? null),
        since,
        age: ageText(since, now),
        // a closed task is not moving: its dots stay ink
        dots: checkpointDots(checkpoints, t.id, now).map((d) => (column === 'done' ? { ...d, recent: false } : d)),
        deps: dependencyRefs(t, byId),
        saved: t.status === 'done' ? null : savedWork(t, state),
        blockedReason: t.status === 'blocked' ? (t.blocked_reason ?? null) : null,
        seal: showSeal
          ? {
              items: report.kind === 'waived' ? [] : sealItems(report, t.acceptance),
              fallback: sealLine(report, t.acceptance),
              reportId: report.id,
              href: receiptHref(project, report.id, t.id),
              review: report.review_state ?? null,
            }
          : null,
        locked: t.acceptance_locked,
      });
    }
    return out;
  }, [tasks, state, reports, live.since, now, checkpoints, byId, project]);

  // -- selection, flows, drag and drop ---------------------------------------------------
  const selected = resolveTaskParam(taskParam, tasks);
  const [flow, setFlow] = useState<Flow>(null);
  const [menu, setMenu] = useState<{ taskId: string; rect: DOMRect } | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const [over, setOver] = useState<{ key: string; ok: boolean } | null>(null);
  const [hot, setHot] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const phone = useIsPhone();
  const [phoneColumnPick, setPhoneColumn] = useState<ColumnId | null>(null);
  // On a phone one column shows at a time: what needs you first, then what is moving.
  const phoneColumn: ColumnId =
    phoneColumnPick ?? (['stalled', 'review', 'progress', 'blocked', 'next', 'done'] as const).find((c) => counts[c] > 0) ?? 'next';

  const go = useCallback(
    (task: string | null, lanes: SwimlaneBy = lanesParam) =>
      navigate('crew', { project, view: 'board', task, lanes: lanes === 'phase' ? null : lanes }, true),
    [project, lanesParam],
  );

  const onChanged = useCallback(() => refreshBoard(), [refreshBoard]);

  const runIntent = useCallback(
    (taskId: string, to: ColumnId) => {
      const task = byId.get(taskId);
      if (!task) return;
      const intent = dropIntent(task, to, human);
      if (intent.kind === 'report') setFlow({ kind: 'report', taskId });
      else if (intent.kind === 'pickup') setFlow({ kind: 'pickup', taskId });
      else if (intent.kind === 'reopen') {
        reopenTask(api, taskId)
          .then(() => {
            toast.success(`T-${task.number} reopened`);
            refreshBoard();
          })
          .catch((err) => toast.error(explainActionError(err)));
      } else if (intent.kind === 'refused') toast.message(intent.reason);
    },
    [byId, human, api, refreshBoard, setFlow],
  );

  const handlers = useMemo<CardHandlers>(
    () => ({
      onOpen: (id) => go(id),
      onPickup: (id) => setFlow({ kind: 'pickup', taskId: id }),
      onReport: (id) => setFlow({ kind: 'report', taskId: id }),
      onMenu: (id, anchor) => setMenu({ taskId: id, rect: anchor.getBoundingClientRect() }),
      onDragStart: (id) => setDragId(id),
      onDragEnd: () => {
        setDragId(null);
        setOver(null);
      },
      onHover: (id) => setHot(id),
    }),
    [go, setFlow, setMenu, setDragId, setOver, setHot],
  );

  const dragTask = dragId ? byId.get(dragId) : undefined;
  const targets = useMemo(() => new Set(dragTask ? dropTargets(dragTask, human) : []), [dragTask, human]);

  const cellProps = (laneKey: string, col: ColumnId) => ({
    'data-target': dragTask ? targets.has(col) : undefined,
    'data-over': over && over.key === `${laneKey}|${col}` ? (over.ok ? 'true' : 'refused') : undefined,
    onDragOver: (e: DragEvent<HTMLDivElement>) => {
      if (!dragTask) return;
      e.preventDefault();
      const ok = targets.has(col);
      e.dataTransfer.dropEffect = ok ? 'move' : 'none';
      const key = `${laneKey}|${col}`;
      if (!over || over.key !== key || over.ok !== ok) setOver({ key, ok });
    },
    onDragLeave: (e: DragEvent<HTMLDivElement>) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver(null);
    },
    onDrop: (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      const id = e.dataTransfer.getData('text/x-crew-task') || dragId;
      setDragId(null);
      setOver(null);
      if (id) runIntent(id, col);
    },
  });

  const menuItems = (taskId: string): MenuItem[] => {
    const t = byId.get(taskId);
    if (!t) return [];
    const items: MenuItem[] = [{ id: 'open', label: 'Open details', hint: 'Criteria, reports, checkpoints', run: () => go(t.id) }];
    if (human && t.status === 'review') items.push({ id: 'review', label: 'Review the report', hint: 'Approve or send back', run: () => setFlow({ kind: 'report', taskId }) });
    if (human && t.status === 'stalled') items.push({ id: 'pickup', label: 'Pick up baton…', hint: 'Hand it to a running agent', run: () => setFlow({ kind: 'pickup', taskId }) });
    if (human && !['done', 'cancelled', 'review'].includes(t.status))
      items.push({ id: 'close', label: 'Close with report or waiver…', hint: 'No report means no Done', run: () => setFlow({ kind: 'report', taskId }) });
    if (human && t.status === 'done') items.push({ id: 'reopen', label: 'Reopen', hint: 'Back to Up next', run: () => runIntent(taskId, 'next') });
    if (t.current_report_id)
      items.push({ id: 'receipt', label: 'Open the receipt', run: () => navigate('crew', { project, view: 'report', report: t.current_report_id, task: t.id }) });
    return items;
  };

  // -- render ---------------------------------------------------------------------------------
  if (board.loading && !board.data) return <TrailSkeleton rows={4} />;
  if (board.error && !board.data) return <ErrorNotice error={board.error} what="the task board" onRetry={board.refresh} />;

  const liveText =
    crew.status === 'live' ? 'live' : crew.status === 'polling' ? 'updating every few seconds' : crew.status === 'resyncing' ? 'catching up' : crew.status;
  const lastMove = live.moves.length ? live.moves[live.moves.length - 1] : null;
  const flowTask = flow && flow.kind !== 'new' ? byId.get(flow.taskId) : undefined;
  const receiptLink = (task: TaskDetail) => (reportId: string) => receiptHref(project, reportId, task.id);
  const layoutKey = `${lanesParam}|${showCancelled}|${tasks.map((t) => `${t.id}:${t.status}:${t.version}`).join(',')}|${[...expanded].join(',')}`;
  const phases = lanes.map((l) => l.title);

  return (
    <div className="crew-board space-y-3">
      <header className="rr-card relative overflow-hidden rounded-[3px]">
        <DitherCloud burst={live.burst} />
        <div className="relative flex flex-wrap items-end justify-between gap-3 px-4 pb-3 pt-4 sm:px-5">
          <div className="min-w-0">
            <p className="rr-eyebrow">Task board · {project}</p>
            <h2 className="font-display mt-1 text-[22px] font-extrabold leading-none tracking-tight text-ink">Tasks only close with a report.</h2>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div role="group" aria-label="Swimlanes" className="flex items-center gap-0.5 rounded-[3px] border border-rule bg-panel p-0.5">
              <Rows3 className="mx-1 h-3.5 w-3.5 text-ink-3" aria-hidden="true" />
              {SWIMLANES.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  aria-pressed={lanesParam === s.id}
                  onClick={() => go(selected?.id ?? null, s.id)}
                  className={clsx(
                    'rounded-[2px] px-2 py-1 font-mono text-[11px]',
                    lanesParam === s.id ? 'bg-ink text-panel' : 'text-ink-2 hover:text-ink',
                  )}
                >
                  {s.label}
                </button>
              ))}
            </div>
            <label className="inline-flex items-center gap-1.5 rounded-[3px] border border-rule bg-panel px-2 py-1 font-mono text-[11px] text-ink-2">
              <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} className="accent-[var(--signal)]" />
              cancelled
            </label>
            {human && (
              <button type="button" onClick={() => setFlow({ kind: 'new' })} className="rr-btn-primary inline-flex items-center gap-1 px-2.5 py-1.5 text-[12.5px]">
                <Plus className="h-3.5 w-3.5" aria-hidden="true" /> New task
              </button>
            )}
          </div>
        </div>
        <div className="relative px-4 pb-6 sm:px-5">
          <StatusStrip live={crew.status === 'live'} statusText={liveText} counts={counts} last={lastMove} />
        </div>
      </header>

      {!!board.error && board.data && <StaleNotice error={board.error} what="the task board" />}
      {!human && detail.data && (
        <p className="px-1 font-mono text-[11px] text-ink-3">Read only: moving tasks, waivers and pickups need a dashboard login with the owner or admin role.</p>
      )}

      {tasks.length === 0 ? (
        <div className="rr-card rounded-[3px] px-4 py-6 text-center sm:px-5">
          <p className="font-display text-lg font-bold text-ink">Tasks only close with a report.</p>
          <p className="mx-auto mt-1 max-w-md text-sm text-ink-2">
            Agents add tasks with <span className="font-mono">remembra-crew task</span> or the crew MCP tools; you can add one here. Each closes when its owner’s report proves the acceptance criteria.
          </p>
          {human && (
            <button type="button" onClick={() => setFlow({ kind: 'new' })} className="rr-btn-primary mt-3 inline-flex items-center gap-1 px-3 py-1.5 text-sm">
              <Plus className="h-3.5 w-3.5" aria-hidden="true" /> New task
            </button>
          )}
        </div>
      ) : phone ? (
        <PhoneBoard
          lanes={lanes}
          counts={counts}
          column={phoneColumn}
          onColumn={setPhoneColumn}
          showLaneTitles={lanesParam !== 'none'}
          cards={cards}
          human={human}
          selectedId={selected?.id ?? null}
          handlers={handlers}
          hotColumns={live.packets.map((p) => p.to)}
        />
      ) : (
        <div className="cb-scroller pb-2">
          <div className="cb-grid" role="group" aria-label="Task board">
            <DependencyLines edges={edges} hot={hot} layoutKey={layoutKey} />
            <div className="col-span-full">
              <PacketTrail packets={live.packets} onDone={live.packetDone} />
            </div>
            {BOARD_COLUMNS.map((c) => (
              <div key={c.id} className="cb-col-head flex items-baseline justify-between gap-2 px-1 pb-1 pt-1.5">
                <h3 className="font-display text-[15px] font-bold text-ink" title={c.hint}>
                  {c.title}
                  <span className="sr-only">: {c.hint}</span>
                </h3>
                <span
                  className={clsx(
                    'tabular font-mono text-[11px]',
                    (c.id === 'stalled' || c.id === 'review') && counts[c.id] > 0 ? 'font-semibold text-signal-ink' : 'text-ink-3',
                  )}
                >
                  {counts[c.id]}
                </span>
              </div>
            ))}
            {lanes.map((lane) => (
              <LaneRows
                key={lane.key}
                laneKey={lane.key}
                title={lanesParam === 'none' ? null : lane.title}
                note={lane.note}
                count={lane.count}
                cells={lane.cells}
                cards={cards}
                human={human}
                selectedId={selected?.id ?? null}
                dragId={dragId}
                handlers={handlers}
                cellProps={cellProps}
                expanded={expanded}
                onExpand={(key) => setExpanded((cur) => new Set([...cur, key]))}
              />
            ))}
          </div>
        </div>
      )}

      {menu && (
        <CardMenu
          label={`Actions for T-${byId.get(menu.taskId)?.number ?? ''}`}
          items={menuItems(menu.taskId)}
          anchor={menu.rect}
          onClose={() => setMenu(null)}
        />
      )}

      {selected && !flow && (
        <TaskDrawer
          api={api}
          task={selected}
          tasks={tasks}
          state={state}
          checkpoints={checkpoints}
          human={human}
          now={now}
          receiptHref={receiptLink(selected)}
          onClose={() => go(null)}
          onChanged={onChanged}
          onReport={() => setFlow({ kind: 'report', taskId: selected.id })}
          onPickup={() => setFlow({ kind: 'pickup', taskId: selected.id })}
        />
      )}
      {flow?.kind === 'report' && flowTask && (
        <CompletionReport api={api} task={flowTask} receiptHref={receiptLink(flowTask)} onClose={() => setFlow(null)} onChanged={onChanged} />
      )}
      {flow?.kind === 'pickup' && flowTask && (
        <PickUpBaton api={api} task={flowTask} state={state} onClose={() => setFlow(null)} onChanged={onChanged} />
      )}
      {flow?.kind === 'new' && (
        <NewTask
          api={api}
          crewId={crewId}
          state={state}
          tasks={tasks}
          defaultPhase={lanesParam === 'phase' && phases.length === 1 && phases[0] !== 'No phase' ? phases[0] : null}
          onClose={() => setFlow(null)}
          onCreated={onChanged}
        />
      )}
    </div>
  );
}

function LaneRows({
  laneKey,
  title,
  note,
  count,
  cells,
  cards,
  human,
  selectedId,
  dragId,
  handlers,
  cellProps,
  expanded,
  onExpand,
}: {
  laneKey: string;
  title: string | null;
  note: string | null;
  count: number;
  cells: Record<ColumnId, TaskDetail[]>;
  cards: Map<string, CardModel>;
  human: boolean;
  selectedId: string | null;
  dragId: string | null;
  handlers: CardHandlers;
  cellProps: (laneKey: string, col: ColumnId) => Record<string, unknown>;
  expanded: Set<string>;
  onExpand: (key: string) => void;
}) {
  return (
    <>
      {title !== null && (
        <div className="cb-lane-head mt-2 flex items-center gap-2 border-t border-rule pb-1 pt-2">
          <span className="rr-rail-h h-[2px] w-5" aria-hidden="true" />
          <h3 className="font-mono text-[11.5px] font-semibold uppercase tracking-[0.08em] text-ink">{title}</h3>
          {note && <span className="font-mono text-[11px] text-ink-3">{note}</span>}
          <span className="font-mono text-[11px] text-ink-3">· {count}</span>
        </div>
      )}
      {COLUMN_IDS.map((col) => {
        const list = cells[col];
        const key = `${laneKey}|${col}`;
        const limit = col === 'done' && !expanded.has(key) ? DONE_VISIBLE : list.length;
        return (
          <div key={col} className="cb-cell space-y-2" aria-label={`${columnTitle(col)}${title ? `, ${title}` : ''}`} role="list" {...cellProps(laneKey, col)}>
            {list.slice(0, limit).map((t) => {
              const card = cards.get(t.id);
              if (!card) return null;
              return (
                <div role="listitem" key={t.id}>
                  <TaskCard card={card} human={human} selected={selectedId === t.id} dragging={dragId === t.id} handlers={handlers} />
                </div>
              );
            })}
            {list.length > limit && (
              <button type="button" onClick={() => onExpand(key)} className="w-full py-1 font-mono text-[11px] text-ink-3 hover:text-ink">
                show {list.length - limit} older
              </button>
            )}
          </div>
        );
      })}
    </>
  );
}

/** The phone check-in layout (§9.12): one column at a time, picked from a tab strip with counts. */
function PhoneBoard({
  lanes,
  counts,
  column,
  onColumn,
  showLaneTitles,
  cards,
  human,
  selectedId,
  handlers,
  hotColumns,
}: {
  lanes: ReturnType<typeof buildLanes>;
  counts: Record<ColumnId, number>;
  column: ColumnId;
  onColumn: (c: ColumnId) => void;
  showLaneTitles: boolean;
  cards: Map<string, CardModel>;
  human: boolean;
  selectedId: string | null;
  handlers: CardHandlers;
  hotColumns: ColumnId[];
}) {
  const shown = lanes.filter((l) => l.cells[column].length > 0);
  return (
    <div className="space-y-2">
      <div role="tablist" aria-label="Columns" className="scrollbar-hide -mx-4 flex gap-1.5 overflow-x-auto px-4 pb-1">
        {BOARD_COLUMNS.map((c) => (
          <button
            key={c.id}
            type="button"
            role="tab"
            aria-selected={column === c.id}
            onClick={(e) => {
              onColumn(c.id);
              e.currentTarget.scrollIntoView({ inline: 'nearest', block: 'nearest' });
            }}
            className={clsx(
              'relative shrink-0 rounded-[3px] border px-2.5 py-1.5 font-mono text-[12px]',
              column === c.id ? 'border-ink bg-ink text-panel' : 'border-rule bg-panel text-ink-2',
              (c.id === 'stalled' || c.id === 'review') && counts[c.id] > 0 && column !== c.id && 'border-signal text-signal-ink',
            )}
          >
            {c.title} <span className="tabular">{counts[c.id]}</span>
            {hotColumns.includes(c.id) && <span aria-hidden="true" className="absolute -right-0.5 -top-0.5 h-1.5 w-1.5 bg-signal" />}
          </button>
        ))}
      </div>
      <div role="tabpanel" aria-label={columnTitle(column)} className="space-y-3">
        {shown.length === 0 && <p className="px-1 py-6 text-center text-sm text-ink-3">Nothing in {columnTitle(column).toLowerCase()}.</p>}
        {shown.map((lane) => (
          <section key={lane.key} aria-label={lane.title}>
            {showLaneTitles && (
              <h3 className="mb-1.5 flex items-center gap-2 font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-ink">
                <span className="rr-rail-h h-[2px] w-4" aria-hidden="true" />
                {lane.title}
              </h3>
            )}
            <div role="list" className="space-y-2">
              {lane.cells[column].map((t) => {
                const card = cards.get(t.id);
                if (!card) return null;
                return (
                  <div role="listitem" key={t.id}>
                    <TaskCard card={card} human={human} selected={selectedId === t.id} dragging={false} handlers={handlers} />
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
