import { describe, expect, it } from 'vitest';
import { pickupSlots, reasonText, reservedTimes, slotSentence, slotZonesText } from '../pickup';
import { claim, event, NOW, state } from './fixture';

describe('pickup slots', () => {
  it('turn a reserved task baton into one slot with its zones, holder, offer and command', () => {
    const s = state();
    const events = [
      event('claim.reserved', 12, { payload: { claim: { id: 'clm_pay' }, reason: 'quota' } }),
      event('baton.ref_created', 12, { payload: { ref: 'refs/remembra/baton/T-3/1', task_id: 'tsk_3', dirty_files: 3, unpushed: 2 } }),
    ];
    const [slot, ...rest] = pickupSlots(s, events);
    expect(rest).toHaveLength(0);
    expect(slot).toMatchObject({
      key: 'task:tsk_3',
      taskRef: 'T-3',
      reason: 'quota',
      reasonText: 'credits ran out',
      fromCallsign: 'cc-2',
      savedFiles: 3,
      unpushed: 2,
      batonRef: 'refs/remembra/baton/T-3/1',
      offeredTo: ['cc-1'],
      pickupCommand: 'remembra-crew adopt T-3',
      parked: false,
    });
    expect(slot.sinceMs).toBe(NOW - 12 * 60000);
    expect(slot.zones).toEqual([{ claimId: 'clm_pay', slug: 'payroll', title: 'Payroll' }]);
    const text = slotSentence(slot, NOW);
    expect(text.lead).toBe('Waiting for the next runner: Payroll, handed off by cc-2 12m ago (credits ran out).');
    expect(text.saved).toBe('3 uncommitted files saved. 2 commits not pushed yet.');
    expect(text.hold).toBe('Held until picked up or released.');
  });

  it('prefers a baton ref already in the reducer state, and falls back to the holder activity time', () => {
    const s = state();
    s.baton_refs = { 'refs/remembra/baton/T-3/1': { seq: 41, task_id: 'tsk_3', session_id: 'cs_c', dirty_files: 1, unpushed: 0 } };
    const [slot] = pickupSlots(s);
    expect(slot.savedFiles).toBe(1);
    expect(slot.sinceMs).toBe(NOW - 12 * 60000); // cs_c last_activity_at
    expect(slotSentence(slot, NOW).saved).toBe('1 uncommitted file saved.');
  });

  it('groups a task’s zones into one baton and keeps idle parks apart', () => {
    const s = state();
    s.claims = {
      ...s.claims,
      clm_pay2: claim('clm_pay2', {
        zone_id: 'zn_rep',
        holder_session_id: 'cs_c',
        task_id: 'tsk_3',
        state: 'reserved',
        reserve_reason: 'quota',
      }),
      clm_idle: claim('clm_idle', { zone_id: 'zn_pos', holder_session_id: 'cs_a', state: 'reserved', reserve_reason: 'idle' }),
    };
    const slots = pickupSlots(s);
    expect(slots.map((x) => x.key)).toEqual(['task:tsk_3', 'hold:cs_a:idle']);
    expect(slotZonesText(slots[0])).toBe('Payroll, Reports');
    const idle = slots[1];
    expect(idle).toMatchObject({ parked: true, pickupCommand: null, task: null });
    expect(slotSentence(idle, Date.parse(s.sessions.cs_a.last_activity_at!) + 3600000).lead).toBe(
      'cc-1 idle 1h; resumes automatically if it comes back.',
    );
    expect(slotSentence(idle, NOW).hold).toBe('Held for the same agent. Only you can hand it on.');
    const offline = pickupSlots({
      ...s,
      claims: {
        x: claim('x', { zone_id: 'zn_rep', holder_session_id: 'cs_b', task_id: 'tsk_2', state: 'reserved', reserve_reason: 'offline' }),
      },
    })[0];
    expect(offline).toMatchObject({ parked: true, pickupCommand: null, taskRef: 'T-2' });
    expect(slotSentence(offline, offline.sinceMs! + 90000).lead).toBe(
      "codex-1's machine offline 1m; it resumes automatically when the machine is back.",
    );
  });

  it('words each reserve reason', () => {
    expect(reasonText('lost', null)).toBe('went silent');
    expect(reasonText('lost', { state_reason: 'process_exited' } as never)).toBe('its process exited');
    expect(reasonText('quota', { state_reason: 'rate_limit' } as never)).toBe('hit a rate limit');
    expect(reasonText('offline', null)).toBe('its machine went offline');
    expect(reasonText('human_hold', null)).toBe('held by a human');
    expect(reasonText(null, null)).toBe('released the baton');
  });

  it('reads reserve times from claim.reserved events', () => {
    const times = reservedTimes([event('claim.reserved', 5, { payload: { claim: { id: 'clm_x' } } }), event('claim.granted', 1)]);
    expect([...times.entries()]).toEqual([['clm_x', NOW - 5 * 60000]]);
  });
});
