// The Policy panel (spec §9.1 `view=policy`): enforcement level (lowering it is
// human-only, D10), the enforcement truth table (§8.5) with each live agent's
// layers, pending and recent zone changes (D9), bypass codes (D34) and the git
// gates per checkout (§8.4), plus the policy log.

import { useState, type ReactNode } from 'react';
import { toast } from 'sonner';
import type { CrewApi } from '../../../lib/crew/api';
import { callsignOf, liveSessions } from '../../../lib/crew/selectors';
import type { CrewDetail, CrewEvent, CrewState, EnforcementLevel } from '../../../lib/crew/types';
import { absoluteTime, relativeTime, shortSha } from '../../../lib/time';
import { policyEvents } from '../zones/eventTail';
import type { ActorAccess } from '../zones/live';
import { PendingZoneChanges } from '../zones/PendingZoneChanges';
import { PixelGlyph } from '../zones/PixelGlyph';
import { ZoneActionConfirm } from '../zones/ZoneActionConfirm';
import type { BypassCodeRow } from '../zones/zonesApi';
import { enforcementLayers, type ZoneChange } from '../zones/zoneModel';
import { BypassCodeDialog } from './BypassCodeDialog';
import { GitHookStatus } from './GitHookStatus';
import {
  ENFORCEMENT_CHOICES,
  TRUST_FOOTNOTE,
  TRUTH_TABLE,
  checkoutRows,
  codeTimeLeft,
  isLowering,
  truthRowFor,
} from './policyModel';
import type { HumanActionRunner } from './useHumanAction';

function Block({ id, eyebrow, title, action, children }: { id: string; eyebrow: string; title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="rr-card rounded-[3px]">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-rule px-4 py-3 sm:px-5">
        <div>
          <p className="rr-eyebrow">{eyebrow}</p>
          <h3 id={id} className="font-display mt-1 text-lg font-bold text-ink">
            {title}
          </h3>
        </div>
        {action}
      </div>
      <div className="px-4 py-4 sm:px-5">{children}</div>
    </section>
  );
}

function EnforcementControl({
  crewId,
  level,
  detail,
  canAdmin,
  why,
  pendingEnforcement,
  api,
  runner,
  onChanged,
}: {
  crewId: string;
  level: EnforcementLevel;
  detail: CrewDetail | undefined;
  canAdmin: boolean;
  why: string | null;
  pendingEnforcement: boolean;
  api: CrewApi;
  runner: HumanActionRunner;
  onChanged: () => void;
}) {
  const [target, setTarget] = useState<EnforcementLevel | null>(null);
  const current = ENFORCEMENT_CHOICES.find((c) => c.value === level);
  return (
    <div>
      <div className="cz-seg" role="group" aria-label="Project enforcement level">
        {ENFORCEMENT_CHOICES.map((c) => (
          <button
            key={c.value}
            type="button"
            aria-pressed={c.value === level}
            disabled={!canAdmin || !detail}
            title={!canAdmin ? (why ?? undefined) : undefined}
            onClick={() => c.value !== level && setTarget(c.value)}
          >
            <span className="block font-semibold">{c.label}</span>
            <span className="block text-[10.5px] opacity-75">{c.value === 'enforce' ? 'default' : 'human only'}</span>
          </button>
        ))}
      </div>
      {current && <p className="mt-2 text-sm text-ink-2">{current.meaning}</p>}
      {!canAdmin && <p className="mt-1 text-xs text-ink-3">{why}</p>}
      {pendingEnforcement && (
        <p className="mt-2 flex items-center gap-2 text-sm text-signal-ink">
          <PixelGlyph name="pending" size={10} /> A zones.yml change to the enforcement level is waiting for your approval below.
        </p>
      )}
      {target && detail && (
        <ZoneActionConfirm
          spec={{
            title: `Set enforcement to ${target}`,
            consequence: ENFORCEMENT_CHOICES.find((c) => c.value === target)!.meaning,
            warning: isLowering(level, target)
              ? 'This lowers protection for every agent on this crew. The change is recorded and announced as a moment.'
              : null,
            confirmLabel: `Set ${target}`,
            danger: isLowering(level, target),
            reason: false,
          }}
          onConfirm={async () => {
            await runner.run('Changing the enforcement level', () => api.patchSettings(crewId, { enforcement: target }, detail.settings_version));
            toast.success(`Enforcement: ${target}`);
            onChanged();
          }}
          onClose={() => setTarget(null)}
        />
      )}
    </div>
  );
}

function codeState(row: BypassCodeRow, nowMs: number, offsetMs: number): { text: string; live: boolean } {
  if (row.state === 'used') return { text: `used ${row.used_at ? relativeTime(row.used_at, new Date(nowMs)) : ''}`, live: false };
  const left = row.state === 'active' ? codeTimeLeft(row.expires_at, nowMs, offsetMs) : null;
  return left ? { text: `active · ${left}`, live: true } : { text: 'expired unused', live: false };
}

function EventRowText({ state, event, now }: { state: CrewState; event: CrewEvent; now: Date }) {
  const who = event.actor?.kind === 'human' ? 'a human' : (event.actor?.callsign ?? callsignOf(state, event.actor?.id) ?? event.actor?.kind);
  const tone = event.type === 'guard.tamper_blocked' || event.type === 'gate.tampered' || event.type === 'githook.missing' ? 'fail' : event.moment ? 'signal' : undefined;
  return (
    <li data-tone={tone} className="py-1">
      <span className="font-mono text-[11.5px] text-ink">{event.summary}</span>
      <span className="ml-2 font-mono text-[10.5px] text-ink-3" title={absoluteTime(event.ts)}>
        {event.type} · {who} · {relativeTime(event.ts, now)}
      </span>
    </li>
  );
}

export function PolicyPanel({
  crewId,
  state,
  detail,
  access,
  canAdmin,
  adminWhy,
  offsetMs,
  now,
  pendingChanges,
  recentChanges,
  codes,
  codesError,
  events,
  api,
  runner,
  onChanged,
  onCodesChanged,
}: {
  crewId: string;
  state: CrewState;
  detail: CrewDetail | undefined;
  access: ActorAccess;
  canAdmin: boolean;
  adminWhy: string | null;
  offsetMs: number;
  now: Date;
  pendingChanges: readonly ZoneChange[];
  recentChanges: readonly ZoneChange[];
  codes: BypassCodeRow[] | undefined;
  codesError: unknown;
  events: readonly CrewEvent[];
  api: CrewApi;
  runner: HumanActionRunner;
  onChanged: () => void;
  onCodesChanged: () => void;
}) {
  const [issuing, setIssuing] = useState(false);
  const level: EnforcementLevel = state.crew?.enforcement ?? 'enforce';
  const sessions = liveSessions(state);
  const pendingEnforcement = pendingChanges.some((c) => c.items.some((i) => i.target === 'enforcement'));
  const decided = recentChanges.filter((c) => c.state !== 'pending').slice(0, 8);
  const log = policyEvents(events);
  const nowMs = now.getTime();

  return (
    <div className="space-y-3">
      <Block id="policy-enforcement" eyebrow="Enforcement" title={`This crew is on ${level}`}>
        <EnforcementControl
          crewId={crewId}
          level={level}
          detail={detail}
          canAdmin={canAdmin}
          why={adminWhy}
          pendingEnforcement={pendingEnforcement}
          api={api}
          runner={runner}
          onChanged={onChanged}
        />
        <div className="mt-5 overflow-x-auto">
          <table className="cz-table">
            <caption className="pb-2 text-left font-mono text-[11px] text-ink-3">Where each kind of agent is stopped</caption>
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Before write</th>
                <th scope="col">At commit</th>
                <th scope="col">At push</th>
                <th scope="col">After the fact</th>
              </tr>
            </thead>
            <tbody>
              {TRUTH_TABLE.map((row, i) => {
                const here = sessions.filter((s) => truthRowFor(s) === i);
                return (
                  <tr key={row.agent} data-live={here.length ? 'true' : undefined}>
                    <td>
                      {row.agent}
                      {here.length > 0 && <span className="mt-0.5 block font-mono text-[11px] text-signal-ink">live: {here.map((s) => s.callsign).join(', ')}</span>}
                    </td>
                    <td>{row.before}</td>
                    <td>{row.commit}</td>
                    <td>{row.push}</td>
                    <td>{row.after}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {sessions.length > 0 && (
          <ul className="mt-4 space-y-1 font-mono text-[12px] text-ink-2">
            {sessions.map((s) => {
              const l = enforcementLayers(s);
              return (
                <li key={s.id}>
                  <span className="text-ink">{s.callsign}</span> · before write: {l.beforeWrite} · commit {l.commit} · push {l.push}
                </li>
              );
            })}
          </ul>
        )}
        <p className="mt-4 border-l-2 border-rule-strong pl-3 text-xs text-ink-3">{TRUST_FOOTNOTE}</p>
      </Block>

      <Block id="policy-changes" eyebrow="Zone policy" title={pendingChanges.length ? `${pendingChanges.length} change${pendingChanges.length === 1 ? '' : 's'} waiting for you` : 'No zone change is waiting'}>
        <PendingZoneChanges
          state={state}
          changes={pendingChanges}
          now={now}
          canAct={access.canAct}
          why={access.why}
          crewApi={api}
          runner={runner}
          onChanged={onChanged}
          heading={false}
        />
        {!pendingChanges.length && (
          <p className="text-sm text-ink-2">
            Changes to <span className="font-mono">.remembra/zones.yml</span> that tighten policy apply at once; any that would loosen it wait here for you.
          </p>
        )}
        {decided.length > 0 && (
          <ul className="cz-trail mt-4">
            {decided.map((c) => (
              <li key={c.id} data-tone={c.state === 'applied' ? 'signal' : undefined} className="py-1 font-mono text-[11.5px]">
                <span className="text-ink">
                  {c.state === 'applied' ? 'approved' : 'rejected'} · zones.yml @ {shortSha(c.yaml_sha) || c.yaml_sha.slice(0, 8)}
                </span>
                <span className="ml-2 text-ink-3" title={absoluteTime(c.decided_at)}>
                  {c.summary} · {c.decided_at ? relativeTime(c.decided_at, now) : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Block>

      <Block
        id="policy-bypass"
        eyebrow="Bypass codes"
        title="Who may pass the gate once"
        action={
          access.canAct ? (
            <button type="button" className="rr-btn-primary px-3 py-1.5 text-sm" onClick={() => setIssuing(true)} disabled={!sessions.length}>
              Issue a code…
            </button>
          ) : undefined
        }
      >
        {!access.canAct ? (
          <p className="text-sm text-ink-3">{access.why}</p>
        ) : codesError ? (
          <p className="text-sm text-fail">Could not load the codes. They are listed only for dashboard logins with an owner or admin role.</p>
        ) : !codes ? (
          <p className="text-sm text-ink-3">Loading…</p>
        ) : !codes.length ? (
          <p className="text-sm text-ink-2">No code has been issued. Only you can open the gate: a code from this panel, or <span className="font-mono">remembra-crew bypass</span> at your own terminal when the server is unreachable. Agents cannot switch it off, and every use is recorded.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="cz-table">
              <thead>
                <tr>
                  <th scope="col">Code</th>
                  <th scope="col">Session</th>
                  <th scope="col">Scope</th>
                  <th scope="col">State</th>
                  <th scope="col">Issued</th>
                </tr>
              </thead>
              <tbody>
                {codes.map((c) => {
                  const st = codeState(c, nowMs, offsetMs);
                  return (
                    <tr key={c.id} data-live={st.live ? 'true' : undefined}>
                      <td className="font-mono">{c.id.slice(0, 12)}</td>
                      <td className="font-mono">{callsignOf(state, c.session_id) ?? '—'}</td>
                      <td className="font-mono">{c.scope}</td>
                      <td className={`font-mono ${st.live ? 'text-signal-ink' : ''}`}>{st.text}</td>
                      <td className="font-mono" title={absoluteTime(c.created_at)}>
                        {relativeTime(c.created_at, now)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Block>

      <Block id="policy-githooks" eyebrow="Git gates" title="Commit and push gates per checkout">
        <GitHookStatus rows={checkoutRows(state)} />
      </Block>

      <Block id="policy-log" eyebrow="Policy log" title="Changes, bypasses and tamper attempts">
        {log.length ? (
          <ul className="cz-trail">
            {log.map((e) => (
              <EventRowText key={e.seq} state={state} event={e} now={now} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-ink-3">Nothing recent.</p>
        )}
      </Block>

      {issuing && (
        <BypassCodeDialog
          crewId={crewId}
          state={state}
          sessions={sessions}
          offsetMs={offsetMs}
          api={api}
          runner={runner}
          onIssued={onCodesChanged}
          onClose={() => setIssuing(false)}
        />
      )}
    </div>
  );
}
