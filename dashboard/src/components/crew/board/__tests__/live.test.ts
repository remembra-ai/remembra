/// <reference types="node" />
// Live check of the Task Board and receipts against a real Remembra server
// (real FastAPI app, crew.db, the WP-6 gate, JWT auth). Driven by
// tests/crew/test_board_live.py, which starts the server, seeds a crew with
// three agent sessions and sets CREW_BOARD_*. Skipped otherwise.
//
// The human side runs exactly the dashboard's code (createCrewApi + the
// board actions and loaders + the model). The agent side is plain HTTP with a
// session token, as the crew CLI and MCP tools send it.

import { writeFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { CrewApiError, createCrewApi } from '../../../../lib/crew/api';
import {
  createTask,
  explainActionError,
  loadBoard,
  loadReceipt,
  loadTaskReports,
  pickUpBaton,
  reopenTask,
  reviewReport,
  saveCriteria,
  waiveCriterion,
  waiveReport,
} from '../actions';
import { acceptanceMeter, buildLanes, dropIntent, mergeTasks, sealItems, sealText, type TaskDetail } from '../model';

const URL_ = process.env.CREW_BOARD_URL ?? '';
const JWT = process.env.CREW_BOARD_JWT ?? '';
const STALE_JWT = process.env.CREW_BOARD_STALE_JWT ?? '';
const CREW = process.env.CREW_BOARD_CREW ?? '';
const TOKENS = JSON.parse(process.env.CREW_BOARD_TOKENS ?? '{}') as Record<string, string>;
const OUT = process.env.CREW_BOARD_OUT ?? '';

const SHA = 'a1b2c3d4e5f60718293a4b5c6d7e8f9012345678';

async function agent<T = Record<string, unknown>>(session: string, method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${URL_}/api/v1${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${JWT}`,
      'X-Remembra-Crew-Session': TOKENS[session],
      'Content-Type': 'application/json',
      'Idempotency-Key': `${session}-${method}-${path}-${Math.random()}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} as ${session}: ${res.status} ${text}`);
  return JSON.parse(text) as T;
}

function byNumber(tasks: TaskDetail[], n: number): TaskDetail {
  const t = tasks.find((x) => x.number === n);
  if (!t) throw new Error(`T-${n} missing`);
  return t;
}

describe.skipIf(!URL_)('live task board', () => {
  it('drives the board, the report gate, reviews, waivers, pickups and receipts against the real server', async () => {
    const api = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: JWT }), fetch: (u, i) => fetch(u, i) });
    const report: Record<string, unknown> = {};

    // -- a person creates the tasks from the board --------------------------------------------
    const t1 = (
      await createTask(api, CREW, {
        title: 'POS split tender',
        phase: 'Phase 2 · POS',
        zone_ids: ['zn_pos'],
        depends_on: [],
        acceptance: [{ id: 'c1', text: 'POS tests pass', kind: 'test', match: 'npm test -- pos', url: null, required: true }],
      })
    ).task!;
    const t2 = (
      await createTask(api, CREW, {
        title: 'Receipt copy reviewed',
        phase: 'Phase 2 · POS',
        zone_ids: ['zn_reports'],
        depends_on: [],
        acceptance: [{ id: 'c1', text: 'Mani likes the copy', kind: 'manual', match: null, url: null, required: true }],
      })
    ).task!;
    const t3 = (await createTask(api, CREW, { title: 'Invoice PDF margins', zone_ids: ['zn_invoices'], depends_on: [], acceptance: [] })).task!;
    const t4 = (
      await createTask(api, CREW, {
        title: 'Payroll export',
        phase: 'Phase 3',
        zone_ids: [],
        depends_on: [t1.id],
        acceptance: [
          { id: 'c1', text: 'export test', kind: 'test', match: 'npm test -- payroll', url: null, required: true },
          { id: 'c2', text: 'looks right', kind: 'manual', match: null, url: null, required: true },
        ],
      })
    ).task!;
    expect([t1.number, t2.number, t3.number, t4.number]).toEqual([1, 2, 3, 4]);
    expect(t4.status).toBe('backlog'); // waits for T-1

    // -- T-1: a hooked session works and reports; the gate accepts on observed evidence --------
    await agent('cs_a', 'POST', `/tasks/${t1.id}/start`, { head: SHA.slice(0, 12) });
    await agent('cs_a', 'POST', `/crews/${CREW}/checkpoints`, {
      session_id: 'cs_a',
      trigger: 'test',
      task_id: t1.id,
      facts: {
        branch: 'main',
        head: SHA,
        tests: [{ command: 'npm test -- pos', passed: 12, failed: 0 }], // no exit_code: see the idempotency note in the WP-13c report
        commits: [{ sha: SHA }],
        pushed: true,
        upstream: 'origin/main',
      },
    });
    const r1 = await agent<{ outcome: string; seal: string }>('cs_a', 'POST', `/tasks/${t1.id}/reports`, {
      sections: { done: ['split tender'] },
      commits: [SHA],
      tests: [{ command: 'npm test -- pos', passed: 12, failed: 0 }],
      summary: 'Split tender done in src/app/pos/Tender.tsx',
    });
    expect(r1.outcome).toBe('accepted');

    // -- T-2: an MCP session's word goes to review under strict reports --------------------------
    await agent('cs_b', 'POST', `/tasks/${t2.id}/start`, {});
    const r2 = await agent<{ outcome: string }>('cs_b', 'POST', `/tasks/${t2.id}/reports`, {
      criteria_evidence: [{ id: 'c1', met: true, note: 'he said so' }],
    });
    expect(r2.outcome).toBe('review');

    // -- T-3: the owner runs out of credits: stalled, zone reserved for the next pickup ----------
    await agent('cs_c', 'POST', `/tasks/${t3.id}/start`, {});
    await agent('cs_c', 'POST', `/sessions/cs_c/stall`, { error: 'billing_error', facts: { branch: 'main', head: SHA } });

    // -- the board, as the dashboard loads and groups it -------------------------------------------
    let board = await loadBoard(api, CREW);
    let tasks = mergeTasks(board.tasks, {});
    expect(byNumber(tasks, 1).status).toBe('done');
    expect(byNumber(tasks, 2).status).toBe('review');
    expect(byNumber(tasks, 3).status).toBe('stalled');
    expect(byNumber(tasks, 4).status).toBe('ready'); // its dependency is done
    expect(byNumber(tasks, 1).done_at).toBeTruthy();
    const lanes = buildLanes(tasks, 'phase', null);
    expect(lanes.map((l) => l.title)).toEqual(['Phase 2 · POS', 'Phase 3', 'No phase']);
    expect(lanes[0].cells.done.map((t) => t.number)).toEqual([1]);
    expect(lanes[0].cells.review.map((t) => t.number)).toEqual([2]);
    expect(lanes[2].cells.stalled.map((t) => t.number)).toEqual([3]);
    expect(board.checkpoints.some((c) => c.task_id === t1.id && c.trigger === 'test')).toBe(true);

    // The Done card's seal: computed per item on the dashboard, identical to the server's seal.
    const reports1 = await loadTaskReports(api, t1.id);
    const current1 = reports1.find((r) => r.id === byNumber(tasks, 1).current_report_id)!;
    expect(current1.review_state).toBe('accepted');
    const items1 = sealItems(current1, byNumber(tasks, 1).acceptance);
    expect(sealText(items1)).toBe(current1.seal);
    expect(items1.find((i) => i.group === 'tests')).toMatchObject({ mark: 'ok', source: 'relay-cli' });
    expect(acceptanceMeter(byNumber(tasks, 1), current1)).toMatchObject({ done: 1, required: 1, reported: true });
    report.seal_t1 = current1.seal;

    // -- "No report means no Done": the server refuses a direct PATCH to done ----------------------
    expect(dropIntent(byNumber(tasks, 4), 'done', true)).toEqual({ kind: 'report', mode: 'waive' });
    const refused = await api.patchTask(t4.id, { status: 'done' }, byNumber(tasks, 4).version).catch((e: unknown) => e);
    expect(refused).toBeInstanceOf(CrewApiError);
    expect((refused as CrewApiError).code).toBe('report_required');
    expect(explainActionError(refused)).toMatch(/No report means no Done/);

    // -- review: the person approves the self-reported report ---------------------------------------
    const reports2 = await loadTaskReports(api, t2.id);
    const pending = reports2.find((r) => r.is_current)!;
    expect(pending.review_state).toBe('review');
    const pendingItems = sealItems(pending, byNumber(tasks, 2).acceptance);
    expect(pendingItems.map((i) => i.text)).toEqual(['manual ✓ (self-reported)', 'pushed ✗']); // deploy_gate.require_pushed
    expect(sealText(pendingItems)).toBe(pending.seal);
    expect(dropIntent(byNumber(tasks, 2), 'done', true)).toEqual({ kind: 'report', mode: 'review' });
    const approved = await reviewReport(api, t2.id, 'approve', 'Copy is right');
    expect(approved.outcome).toBe('approved');
    expect(approved.task?.status).toBe('done');

    // -- pick up the stalled baton: hand T-3 to codex-1 ------------------------------------------------
    expect(dropIntent(byNumber(tasks, 3), 'progress', true)).toEqual({ kind: 'pickup' });
    const picked = await pickUpBaton(api, t3.id, 'cs_b');
    expect(picked.task?.owner_session_id).toBe('cs_b');
    expect(['claimed', 'in_progress']).toContain(picked.task?.status);
    const batons3 = (await api.batons(CREW, t3.id)) as { batons: { kind: string; to_session: string }[] };
    expect(batons3.batons.some((b) => b.kind === 'human_assign' && b.to_session === 'cs_b')).toBe(true);

    // -- waivers: a stale login is asked to sign in again; a fresh one waives --------------------------
    await agent('cs_a', 'POST', `/tasks/${t4.id}/start`, {});
    const staleApi = createCrewApi({ baseUrl: URL_, credentials: () => ({ jwt: STALE_JWT }), fetch: (u, i) => fetch(u, i) });
    const stepUp = await waiveCriterion(staleApi, t4.id, 'c2', 'checked by hand').catch((e: unknown) => e);
    expect(stepUp).toBeInstanceOf(CrewApiError);
    expect((stepUp as CrewApiError).stepUpRequired).toBe(true);
    expect(explainActionError(stepUp)).toMatch(/Sign in again/);

    const waivedOne = await waiveCriterion(api, t4.id, 'c2', 'Checked by hand on staging');
    expect(waivedOne.outcome).toBe('criterion_waived');
    board = await loadBoard(api, CREW);
    tasks = mergeTasks(board.tasks, {});
    const t4Now = byNumber(tasks, 4);
    expect(t4Now.acceptance.find((c) => c.id === 'c2')?.waived?.reason).toBe('Checked by hand on staging');
    expect(acceptanceMeter(t4Now, null)).toMatchObject({ done: 1, required: 2, waived: 1 });

    // Criteria stay editable by the person after the lock (If-Match), and a stale version is refused.
    expect(t4Now.acceptance_locked).toBe(true);
    const edited = await saveCriteria(
      api,
      t4.id,
      [
        { id: 'c1', text: 'export test passes', kind: 'test', match: 'npm test -- payroll', url: null, required: true },
        { id: 'c2', text: 'looks right', kind: 'manual', match: null, url: null, required: true },
      ],
      t4Now.version,
    );
    expect(edited.task?.acceptance[0].text).toBe('export test passes');
    const conflict = await saveCriteria(api, t4.id, [], t4Now.version).catch((e: unknown) => e);
    expect((conflict as CrewApiError).status).toBe(412);
    expect(explainActionError(conflict)).toMatch(/changed this task/);

    const waivedAll = await waiveReport(api, t4.id, 'Shipping without the export test; tracked in T-9');
    expect(waivedAll.outcome).toBe('waived');
    expect(waivedAll.task?.status).toBe('done');
    expect(waivedAll.report?.kind).toBe('waived');

    // -- receipts ------------------------------------------------------------------------------------------
    const receipt4 = await loadReceipt(api, CREW, waivedAll.report!.id, t4.id);
    expect(receipt4.report.kind).toBe('waived');
    expect(receipt4.report.review_note).toBe('Shipping without the export test; tracked in T-9');
    expect(receipt4.task.id).toBe(t4.id);

    // Reopen T-1, then open its old report from a link that only carries the report id.
    const reopened = await reopenTask(api, t1.id);
    expect(['ready', 'reopened', 'backlog']).toContain(reopened.task?.status);
    const receipt1 = await loadReceipt(api, CREW, current1.id, null);
    expect(receipt1.task.id).toBe(t1.id);
    expect(receipt1.report.id).toBe(current1.id);
    expect(receipt1.history.length).toBeGreaterThanOrEqual(1);
    expect(receipt1.report.grounding?.status).toBeTruthy();

    const receipt3 = await loadReceipt(api, CREW, (await loadTaskReports(api, t3.id))[0].id, t3.id);
    expect(receipt3.batons.some((b) => b.kind === 'human_assign')).toBe(true);
    const missing = await loadReceipt(api, CREW, 'rep_does_not_exist', null).catch((e: unknown) => e);
    expect((missing as Error).name).toBe('ReceiptNotFound');

    report.t3_status = picked.task?.status;
    report.t4_report = waivedAll.report?.id;
    report.t1_report = current1.id;
    if (OUT) writeFileSync(OUT, JSON.stringify(report));
  }, 60_000);
});
