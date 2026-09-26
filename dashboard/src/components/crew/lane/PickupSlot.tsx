// A pickup slot (spec §9.3, §9.4): the dashed empty lane where a stopped
// agent's zones wait, reserved, for the next runner. The baton lies at the
// start of the track in a haze of dithered pixels.
//
//   Waiting for the next runner: POS section, handed off by cc-2 12m ago
//   (credits ran out). 3 uncommitted files saved. Held until picked up or
//   released. [Hand baton to…] [Copy pickup command] [Release]

import clsx from 'clsx';
import { useCopy } from '../../../hooks/useCopy';
import { agentMeta } from '../../../lib/agents';
import { presenceText } from '../../../lib/crew/selectors';
import type { CrewState } from '../../../lib/crew/types';
import type { ActionRequest } from './ActionDialog';
import { DitherField } from './DitherField';
import { slotSentence, type PickupSlotView } from './pickup';
import './lane.css';

export function PickupSlot({
  slot,
  state,
  nowMs,
  canAct,
  onRequest,
}: {
  slot: PickupSlotView;
  state: CrewState;
  nowMs: number;
  canAct: boolean;
  onRequest: (request: ActionRequest) => void;
}) {
  const [copy] = useCopy();
  const text = slotSentence(slot, nowMs);
  const claimIds = slot.claims.map((c) => c.id);
  const zonesText = slot.zones.map((z) => z.slug).join(', ');
  const targets = Object.values(state.sessions)
    .filter((s) => s.id !== slot.fromSessionId && !['ended', 'lost', 'quota_blocked'].includes(s.state))
    .map((s) => ({ id: s.id, label: s.callsign, detail: `${agentMeta(s.agent_id).name} · ${presenceText(s)}` }));
  const subject = slot.taskRef ? `${slot.taskRef} baton` : `${zonesText} baton`;
  const humanHint = canAct ? undefined : 'Needs a dashboard login';

  return (
    <section
      data-slot={slot.key}
      data-slot-session={slot.fromSessionId ?? undefined}
      data-idle={slot.parked ? 'true' : undefined}
      aria-label={`Pickup slot: ${zonesText}${slot.taskRef ? ` for ${slot.taskRef}` : ''}, ${slot.parked ? 'parked for the same agent' : 'waiting for the next agent'}`}
      className="crew-slot overflow-hidden"
    >
      <DitherField shape="slot" seed={slot.key.length} animate={!slot.parked} />
      <div className="relative px-5 py-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span aria-hidden="true" className={clsx('crew-baton-glyph', slot.parked && 'opacity-40')} />
          <p className="rr-eyebrow">{slot.parked ? 'Parked' : 'Pickup'}</p>
          <p className="font-mono text-[12px] font-semibold text-ink">
            {slot.zones.map((z) => (
              <span key={z.claimId} className="mr-1.5 inline-block">
                <span className="text-signal">✦</span> {z.slug.toUpperCase()}
              </span>
            ))}
          </p>
          {slot.taskRef && <span className="font-mono text-[12px] text-ink-2">{slot.taskRef}</span>}
          {slot.task?.title && <span className="min-w-0 truncate text-sm text-ink-2">{slot.task.title}</span>}
        </div>
        <p className="mt-2 max-w-[62ch] text-sm leading-relaxed text-ink">
          {text.lead} {text.saved && <span className="font-semibold">{text.saved}</span>} <span className="text-ink-2">{text.hold}</span>
        </p>
        <div aria-hidden="true" className="relative mt-3 flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-ink-3">{slot.fromCallsign ?? 'baton'}</span>
          <span className="relative h-2 flex-1">
            <span className="crew-track absolute inset-x-0 top-1/2 -translate-y-1/2" />
          </span>
          <span className="h-2.5 w-2.5 border-2 border-dashed border-signal" />
          <span className="font-mono text-[10px] uppercase tracking-[0.08em] text-signal-ink">
            {slot.parked ? 'same agent' : 'next runner'}
          </span>
        </div>
        {slot.offeredTo.length > 0 && (
          <p className="mt-1 font-mono text-[11px] text-ink-3">offered to {slot.offeredTo.join(', ')} in its brief</p>
        )}
        {slot.batonRef && <p className="mt-1 truncate font-mono text-[11px] text-ink-3">saved as {slot.batonRef}</p>}
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            aria-disabled={!canAct}
            title={humanHint}
            onClick={() =>
              canAct &&
              onRequest({
                input: { action: 'hand-baton', taskId: slot.task?.id ?? null, claimIds },
                subject,
                targets,
              })
            }
            className={clsx('rr-btn-primary px-3 py-1.5 text-sm', !canAct && 'opacity-50')}
          >
            Hand baton to…
          </button>
          {slot.pickupCommand && (
            <button
              type="button"
              onClick={() => copy(slot.pickupCommand!, 'Pickup command copied')}
              className="rr-btn-ghost px-3 py-1.5 font-mono text-[12px]"
            >
              Copy pickup command
            </button>
          )}
          <button
            type="button"
            aria-disabled={!canAct}
            title={humanHint}
            onClick={() => canAct && onRequest({ input: { action: 'release-baton', claimIds }, subject })}
            className={clsx('rr-btn-ghost px-3 py-1.5 text-sm', !canAct && 'opacity-50')}
          >
            Release
          </button>
        </div>
      </div>
    </section>
  );
}
