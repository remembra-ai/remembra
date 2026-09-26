// Board and receipt I/O over the crew API client (spec §6 "Tasks"): what the
// Task Board loads, and the human actions it offers. Every function takes the
// CrewApi so the live test drives exactly this code against a real server.
//
// Human-only routes (D27): assign (pick up baton), review, waive (step-up).
// Reopen and acceptance edits are allowed for a human principal; after the
// criteria lock only a human (owner/admin) may change them.

import { CrewApiError, type CrewApi } from '../../../lib/crew/api';
import type { Criterion } from '../../../lib/crew/types';
import type { BatonRow, CheckpointDetail, ReportDetail, TaskDetail } from './model';

export interface BoardData {
  tasks: TaskDetail[];
  checkpoints: CheckpointDetail[];
}

/** Every task of the crew (≤500) plus the latest checkpoints (≤500) for the dots. */
export async function loadBoard(api: CrewApi, crewId: string): Promise<BoardData> {
  const [tasks, checkpoints] = await Promise.all([
    api.tasks(crewId) as Promise<{ tasks?: TaskDetail[] }>,
    api.request<{ checkpoints?: CheckpointDetail[] }>(`/crews/${encodeURIComponent(crewId)}/checkpoints`, { query: { limit: 500 } }),
  ]);
  return { tasks: tasks.tasks ?? [], checkpoints: checkpoints.data?.checkpoints ?? [] };
}

export async function loadTaskReports(api: CrewApi, taskId: string): Promise<ReportDetail[]> {
  const res = await api.taskReports(taskId);
  return (res.reports ?? []) as ReportDetail[];
}

export async function loadTask(api: CrewApi, taskId: string): Promise<TaskDetail> {
  const res = (await api.task(taskId)) as { task: TaskDetail };
  return res.task;
}

export async function loadBatons(api: CrewApi, crewId: string, taskId: string): Promise<BatonRow[]> {
  const res = (await api.batons(crewId, taskId)) as { batons?: BatonRow[] };
  return res.batons ?? [];
}

export interface Receipt {
  task: TaskDetail;
  report: ReportDetail;
  /** Every report of the task, newest first (the current one included). */
  history: ReportDetail[];
  batons: BatonRow[];
}

export class ReceiptNotFound extends Error {
  constructor(reportId: string) {
    super(`Report ${reportId} was not found in this crew.`);
    this.name = 'ReceiptNotFound';
  }
}

/**
 * Load a receipt. `taskId` comes from the link or from what the screen
 * already knows; without it the tasks that can carry a report are searched
 * (bounded, newest first).
 */
export async function loadReceipt(
  api: CrewApi,
  crewId: string,
  reportId: string,
  taskId: string | null,
  candidates: readonly TaskDetail[] = [],
): Promise<Receipt> {
  const tryTask = async (tid: string): Promise<Receipt | null> => {
    let reports: ReportDetail[];
    try {
      reports = await loadTaskReports(api, tid);
    } catch (err) {
      if (err instanceof CrewApiError && err.status === 404) return null;
      throw err;
    }
    const report = reports.find((r) => r.id === reportId);
    if (!report) return null;
    const [task, batons] = await Promise.all([loadTask(api, tid), loadBatons(api, crewId, tid)]);
    const history = [...reports].sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
    return { task, report, history, batons };
  };
  if (taskId) {
    const found = await tryTask(taskId);
    if (found) return found;
  }
  // Tasks that can hold this report: every task that ever reached in_progress. Search the likely
  // ones first (a current report, then report-bearing states), newest change first; bounded.
  const REPORT_STATES = ['done', 'review', 'stalled', 'in_progress', 'blocked', 'cancelled'];
  const rank = (t: TaskDetail) => (t.current_report_id ? 0 : REPORT_STATES.includes(t.status) ? 1 : t.started_at ? 1 : 2);
  let pool = candidates.length ? [...candidates] : (((await api.tasks(crewId)) as { tasks?: TaskDetail[] }).tasks ?? []);
  pool = pool
    .filter((t) => t.id !== taskId && t.status !== 'backlog')
    .sort((a, b) => rank(a) - rank(b) || (b.updated_at ?? '').localeCompare(a.updated_at ?? '') || b.number - a.number)
    .slice(0, 60);
  const direct = pool.find((t) => t.current_report_id === reportId);
  if (direct) {
    const found = await tryTask(direct.id);
    if (found) return found;
  }
  for (const t of pool) {
    if (t === direct) continue;
    const found = await tryTask(t.id);
    if (found) return found;
  }
  throw new ReceiptNotFound(reportId);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export interface ActionResult {
  task: TaskDetail | null;
  report: ReportDetail | null;
  seq: number | null;
  outcome: string | null;
}

function result(raw: Record<string, unknown>): ActionResult {
  return {
    task: (raw.task as TaskDetail | undefined) ?? null,
    report: raw.report && typeof raw.report === 'object' && 'id' in (raw.report as object) ? (raw.report as ReportDetail) : null,
    seq: typeof raw.seq === 'number' ? raw.seq : null,
    outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
  };
}

/** (H, step-up) Waive one criterion: the next report counts it as waived. */
export async function waiveCriterion(api: CrewApi, taskId: string, criterionId: string, reason: string): Promise<ActionResult> {
  return result(await api.waiveTask(taskId, criterionId, reason.trim()));
}

/** (H, step-up) Waive the whole report: a `waived` report with the reason, and the task is done (D17). */
export async function waiveReport(api: CrewApi, taskId: string, reason: string): Promise<ActionResult> {
  return result(await api.waiveTask(taskId, 'all', reason.trim()));
}

/** (H) Approve (→ done) or reject (→ in progress) the report in review. */
export async function reviewReport(api: CrewApi, taskId: string, decision: 'approve' | 'reject', note?: string): Promise<ActionResult> {
  const text = note?.trim();
  return result(await api.reviewTask(taskId, decision, text ? text : undefined));
}

/** (H) Pick up a stalled baton: assign the task to a live session (records an offer and a baton). */
export async function pickUpBaton(api: CrewApi, taskId: string, sessionId: string): Promise<ActionResult> {
  return result(await api.assignTask(taskId, sessionId));
}

export async function reopenTask(api: CrewApi, taskId: string): Promise<ActionResult> {
  return result(await api.reopenTask(taskId));
}

/** Replace the acceptance criteria (If-Match: the task version). After the lock this is human-only. */
export async function saveCriteria(api: CrewApi, taskId: string, criteria: Criterion[], version: number): Promise<ActionResult> {
  return result(await api.patchTask(taskId, { acceptance: criteria }, version));
}

export interface NewTaskInput {
  title: string;
  phase?: string;
  body?: string;
  zone_ids: string[];
  acceptance: Criterion[];
  depends_on: string[];
  priority?: number;
}

export async function createTask(api: CrewApi, crewId: string, input: NewTaskInput): Promise<ActionResult> {
  const body: Parameters<CrewApi['createTask']>[1] = {
    title: input.title.trim(),
    zone_ids: input.zone_ids,
    acceptance: input.acceptance,
    depends_on: input.depends_on,
  };
  if (input.phase?.trim()) body.phase = input.phase.trim();
  if (input.body?.trim()) body.body = input.body.trim();
  if (input.priority !== undefined) body.priority = input.priority;
  return result(await api.createTask(crewId, body));
}

/** One sentence saying why an action failed and what to do about it. */
export function explainActionError(err: unknown): string {
  if (err instanceof CrewApiError) {
    if (err.stepUpRequired) return 'Sign in again to confirm this. Waivers need a login from the last 15 minutes.';
    if (err.humanOnly) return 'This needs a dashboard login. API keys never act as a person.';
    if (err.status === 412) return 'Someone changed this task a moment ago. The board has the new version; try again.';
    if (err.status === 428) return 'The task version was missing. Reload the board and try again.';
    if (err.status === 0) return 'Could not reach the Remembra server. Check the connection and try again.';
    if (err.code === 'report_required') return 'No report means no Done. Waive the report with a reason, or wait for the agent to report.';
    if (err.code === 'session_not_live') return `${err.message} Pick a session that is running.`;
    if (err.code === 'crew_role_required') return 'Only a crew owner or admin can do this.';
    if (err.status === 429) return `Too many actions at once. Try again in ${err.retryAfterS ?? 'a few'} seconds.`;
    return err.message || `The server refused this (${err.code}).`;
  }
  return err instanceof Error ? err.message : String(err);
}
