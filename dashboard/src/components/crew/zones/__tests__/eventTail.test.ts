import { describe, expect, it } from 'vitest';
import type { CrewEvent } from '../../../../lib/crew/types';
import { EventTailStore, mergeTail, policyEvents, zoneClaimEvents, zoneNearMisses, type EventTailApi } from '../eventTail';

function ev(seq: number, type = 'claim.granted', extra: Partial<CrewEvent> = {}): CrewEvent {
  return {
    seq,
    id: `evt_${seq}`,
    crew_id: 'crw_1',
    project_id: 'p',
    ts: `2026-09-26T14:00:${String(seq % 60).padStart(2, '0')}Z`,
    type,
    v: 1,
    origin: 'server',
    actor: { kind: 'session', id: 'cs_a', callsign: 'cc-1', verified: true },
    refs: { zone_id: 'zn_pos' },
    severity: 'info',
    moment: false,
    summary: `${type} #${seq}`,
    payload: {},
    ...extra,
  };
}

/** A fake events endpoint over an in-memory log, recording every request. */
class FakeLog implements EventTailApi {
  log: CrewEvent[] = [];
  calls: number[] = [];
  fail = false;
  private waiting: (() => void)[] = [];
  gate = false;

  add(n: number, type?: string) {
    const start = this.log.length;
    for (let i = 1; i <= n; i++) this.log.push(ev(start + i, type));
  }

  release() {
    const w = this.waiting;
    this.waiting = [];
    for (const r of w) r();
  }

  eventsPage = async (_crewId: string, sinceSeq: number, limit = 200) => {
    this.calls.push(sinceSeq);
    if (this.gate) await new Promise<void>((r) => this.waiting.push(r));
    if (this.fail) throw new Error('down');
    const after = this.log.filter((e) => e.seq > sinceSeq);
    const events = after.slice(0, limit);
    return { events, last_seq: this.log.length, has_more: after.length > limit };
  };
}

describe('event tail', () => {
  it('merges pages in seq order without duplicates and keeps the last window', () => {
    const merged = mergeTail([ev(1), ev(2), ev(3)], [ev(3), ev(4)], 3);
    expect(merged.map((e) => e.seq)).toEqual([2, 3, 4]);
  });

  it('backfills the last window, then follows the live seq one page at a time', async () => {
    const api = new FakeLog();
    api.add(650);
    const store = new EventTailStore(api, 'crw_1', 400);
    await store.want(650);
    // backfill from 650-400=250: two pages (251..450, 451..650)
    expect(api.calls).toEqual([250, 450]);
    const view = store.getView();
    expect(view.loading).toBe(false);
    expect(view.events.length).toBe(400);
    expect(view.events[0].seq).toBe(251);
    expect(view.fromSeq).toBe(251);

    api.add(3);
    await store.want(653);
    expect(api.calls).toEqual([250, 450, 650]);
    expect(store.getView().events.at(-1)!.seq).toBe(653);
    expect(store.getView().events.length).toBe(400);
    expect(store.getView().fromSeq).toBe(254); // trimmed: the window moved on

    // nothing new: no request
    await store.want(653);
    expect(api.calls.length).toBe(3);
  });

  it('never runs two requests at once; a seq that moves meanwhile is fetched after', async () => {
    const api = new FakeLog();
    api.add(5);
    const store = new EventTailStore(api, 'crw_1', 400);
    api.gate = true;
    const first = store.want(5);
    api.add(2);
    const second = store.want(7); // arrives while the first request is in flight
    expect(api.calls).toEqual([0]);
    api.gate = false;
    api.release();
    await first;
    await second;
    // the in-flight run saw has_more=false with last_seq 7 already → covered in one request
    expect(api.calls).toEqual([0]);
    expect(store.getView().events.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it('reports a failure, keeps what it had and retries on the next want', async () => {
    const api = new FakeLog();
    api.add(3);
    const store = new EventTailStore(api, 'crw_1', 400);
    await store.want(3);
    api.add(2);
    api.fail = true;
    await store.want(5);
    expect(store.getView().error).toBeInstanceOf(Error);
    expect(store.getView().events.length).toBe(3);
    api.fail = false;
    await store.want(5);
    expect(store.getView().error).toBeNull();
    expect(store.getView().events.length).toBe(5);
  });

  it('stops fetching while stopped and resumes on start', async () => {
    const api = new FakeLog();
    api.add(2);
    const store = new EventTailStore(api, 'crw_1', 400);
    store.stop();
    await store.want(2);
    expect(api.calls).toEqual([]);
    store.start();
    await store.want(2);
    expect(store.getView().events.length).toBe(2);
    let notified = 0;
    const off = store.subscribe(() => notified++);
    api.add(1);
    await store.want(3);
    expect(notified).toBe(1);
    off();
  });

  it('picks the claim history and near-misses of one zone, newest first', () => {
    const events = [
      ev(1, 'claim.granted'),
      ev(2, 'guard.blocked', { refs: {}, payload: { zone: 'pos', path_rel: 'src/app/pos/a.ts' } }),
      ev(3, 'claim.granted', { refs: { zone_id: 'zn_other' } }),
      ev(4, 'claim.released'),
      ev(5, 'guard.blocked', { refs: { zone_id: 'zn_pos' } }),
      ev(6, 'guard.blocked', { refs: {}, payload: { zone: 'reports' } }),
      ev(7, 'zone.frozen'),
    ];
    expect(zoneClaimEvents(events, 'zn_pos').map((e) => e.seq)).toEqual([4, 1]);
    expect(zoneClaimEvents(events, 'zn_pos', 1).map((e) => e.seq)).toEqual([4]);
    expect(zoneNearMisses(events, { id: 'zn_pos', slug: 'pos' }).map((e) => e.seq)).toEqual([5, 2]);
    expect(policyEvents([ev(1, 'guard.bypass_used'), ev(2, 'claim.granted'), ev(3, 'githook.missing'), ev(4, 'zone.change_pending')]).map((e) => e.seq)).toEqual([4, 3, 1]);
  });
});
