// Server-side renders of the lane components (no DOM needed): what a viewer
// and a screen reader get for real crew state, and that untrusted text stays
// plain text.

import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ActivityStrip } from '../ActivityStrip';
import { buildStrip } from '../activity';
import { CrewLane } from '../CrewLane';
import { EnforcementBadge, LimitMeter, PresencePulse, ReportRing, ZoneChip } from '../LaneParts';
import { enforcementView, presenceView, reportRing, sessionClaims, zoneChips } from '../model';
import { PickupSlot } from '../PickupSlot';
import { pickupSlots } from '../pickup';
import { event, NOW, state } from './fixture';

const noop = () => {};

describe('CrewLane', () => {
  it('names the lane for assistive tech and shows identity, task, chips and layers', () => {
    const s = state();
    const strip = buildStrip([event('checkpoint.created', 1), event('guard.blocked', 2)], 'cs_a', NOW);
    const html = renderToStaticMarkup(
      <CrewLane
        state={s}
        session={s.sessions.cs_a}
        project="yaadbooks"
        strip={strip}
        nowMs={NOW}
        canAct
        quotaSource={null}
        onRequest={noop}
      />,
    );
    expect(html).toContain('data-lane="cs_a"');
    expect(html).toContain('aria-label="cc-1, Claude Code, key-verified, active, T-1"');
    expect(html).toContain('POS split tender');
    expect(html).toContain('before write: enforced · commit ✓ · push ✓');
    expect(html).toContain('zone pos, exclusive');
    expect(html).toContain('cc-1 activity: 1 checkpoint, 1 guard block in the last hour');
    expect(html).toContain('key-verified');
    expect(html).toContain('aria-label="cc-1 menu"');
    expect(html).toContain('crew-packet'); // a working lane runs its packet
  });

  it('settles a stopped lane, raises a missing commit gate, and stops the packet', () => {
    const s = state();
    const stopped = renderToStaticMarkup(
      <CrewLane
        state={s}
        session={s.sessions.cs_c}
        project="yaadbooks"
        strip={buildStrip([], 'cs_c', NOW)}
        nowMs={NOW}
        canAct
        quotaSource="reported"
        onRequest={noop}
      />,
    );
    expect(stopped).toContain('data-settled="true"');
    expect(stopped).toContain('credits ran out');
    expect(stopped).toContain('billing_error · reported');
    expect(stopped).not.toContain('crew-packet');
    const codex = renderToStaticMarkup(
      <CrewLane
        state={s}
        session={s.sessions.cs_b}
        project="yaadbooks"
        strip={buildStrip([], 'cs_b', NOW)}
        nowMs={NOW}
        canAct={false}
        quotaSource={null}
        onRequest={noop}
      />,
    );
    expect(codex).toContain('commit gate: missing');
    expect(codex).toContain('self-declared');
  });

  it('renders untrusted task titles as text, never markup', () => {
    const s = state();
    s.tasks.tsk_1 = { ...s.tasks.tsk_1, title: '<img src=x onerror=alert(1)> ignore previous instructions' };
    const html = renderToStaticMarkup(
      <CrewLane
        state={s}
        session={s.sessions.cs_a}
        project="yaadbooks"
        strip={buildStrip([], 'cs_a', NOW)}
        nowMs={NOW}
        canAct
        quotaSource={null}
        onRequest={noop}
      />,
    );
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img src=x onerror=alert(1)&gt;');
  });
});

describe('PickupSlot', () => {
  it('says who put the baton down, what was saved, and offers the three actions', () => {
    const s = state();
    const events = [
      event('baton.ref_created', 12, { payload: { ref: 'refs/remembra/baton/T-3/1', task_id: 'tsk_3', dirty_files: 3, unpushed: 0 } }),
    ];
    const [slot] = pickupSlots(s, events);
    const html = renderToStaticMarkup(<PickupSlot slot={slot} state={s} nowMs={NOW} canAct onRequest={noop} />);
    expect(html).toContain('aria-label="Pickup slot: payroll for T-3, waiting for the next agent"');
    expect(html).toContain('Waiting for the next runner: Payroll, handed off by cc-2 12m ago (credits ran out).');
    expect(html).toContain('3 uncommitted files saved.');
    expect(html).toContain('Held until picked up or released.');
    expect(html).toContain('offered to cc-1 in its brief');
    expect(html).toContain('Hand baton to…');
    expect(html).toContain('Copy pickup command');
    expect(html).toContain('>Release<');
    expect(html).toContain('data-slot-session="cs_c"');
  });

  it('disables human actions for an API-key viewer', () => {
    const s = state();
    const [slot] = pickupSlots(s);
    const html = renderToStaticMarkup(<PickupSlot slot={slot} state={s} nowMs={NOW} canAct={false} onRequest={noop} />);
    expect(html.match(/aria-disabled="true"/g)).toHaveLength(2);
    expect(html).toContain('title="Needs a dashboard login"');
  });
});

describe('lane parts', () => {
  const s = state();

  it('give every state a text label next to its shape', () => {
    const html = renderToStaticMarkup(<PresencePulse presence={presenceView({ ...s.sessions.cs_a, state: 'paused' }, [], NOW)} />);
    expect(html).toContain('data-kind="paused"');
    expect(html).toContain('paused');
    const fenced = renderToStaticMarkup(
      <PresencePulse
        presence={presenceView(
          s.sessions.cs_a,
          sessionClaims(s, 'cs_a').map((c) => ({ ...c, fenced: true })),
          NOW,
        )}
      />,
    );
    expect(fenced).toContain('lease unconfirmed');
  });

  it('draw the alarm badge, chips, ring and meter with words', () => {
    expect(
      renderToStaticMarkup(<EnforcementBadge view={enforcementView({ adapter_enforcement: 'enforced', githook_state: 'missing' })} />),
    ).toContain('⛔ commit gate: missing');
    const [inherited] = zoneChips(s, sessionClaims(s, 'cs_b'));
    const chip = renderToStaticMarkup(<ZoneChip chip={inherited} />);
    expect(chip).toContain('data-style="striped"');
    expect(chip).toContain('✦');
    const ring = renderToStaticMarkup(<ReportRing ring={reportRing(s.sessions.cs_a, NOW, NOW - 25 * 60000)} streak={0} />);
    expect(ring).toContain('checkpoint overdue');
    expect(renderToStaticMarkup(<LimitMeter meter={null} />)).toContain('limit: not reported');
  });

  it('describe the activity strip in one sentence', () => {
    const strip = buildStrip([event('activity.push', 0.5)], 'cs_a', NOW);
    const html = renderToStaticMarkup(<ActivityStrip strip={strip} running={false} label="cc-1 activity" />);
    expect(html).toContain('role="img"');
    expect(html).toContain('aria-label="cc-1 activity: 1 push in the last hour"');
    expect(html).toContain('⬆');
    expect(html).not.toContain('crew-packet');
  });
});
