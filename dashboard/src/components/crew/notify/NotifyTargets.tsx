// Real-time targets (§9.11): where Remembra reaches the owner the moment an
// agent stops, collides, or tries to switch protection off. Human only:
// an instant email, or an https webhook that must answer a signed challenge
// before it is saved (Mani's Clawdbot/Telegram bridge consumes it). The
// signing secret is shown exactly once.

import { useId, useState, type FormEvent } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { useResource } from '../../../hooks/useResource';
import { crewApi } from '../../../lib/crew/api';
import { useCrewList } from '../../../lib/crew/hooks';
import { absoluteTime } from '../../../lib/time';
import { CopyCommand, Pill } from '../../relay/ui';
import { PixelGlyph } from '../channel/pixels';
import { actionError } from '../channel/useChannel';
import { kindTitle, targetProblem, webhookRecipe, type AddedTarget, type NotificationRules } from './model';

function TurnOn({ crewId, project, kind, onDone }: { crewId: string; project: string; kind: 'email' | 'webhook'; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const turnOn = async () => {
    setBusy(true);
    setError(null);
    try {
      const detail = await crewApi.getCrew(crewId);
      const current = ((detail.settings.notify as { realtime?: string[] } | undefined)?.realtime ?? []).filter((k) => k === 'email' || k === 'webhook');
      await crewApi.patchSettings(crewId, { notify: { realtime: [...new Set([...current, kind])] } }, detail.settings_version);
      toast.success(`${project} now sends ${kind} alerts.`);
      onDone();
    } catch (err) {
      setError(actionError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-ink-2">
        <span className="font-mono font-bold text-ink">{project}</span> does not send {kind} alerts yet.
      </span>
      <button type="button" onClick={() => void turnOn()} disabled={busy} className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-xs">
        {busy && <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />} Turn on for {project}
      </button>
      {error && <span className="basis-full text-xs text-fail">{error}</span>}
    </li>
  );
}

/** An email target waiting for the code we mailed to it: alerts go only to confirmed addresses. */
function ConfirmEmail({ targetId, onDone }: { targetId: string; onDone: () => void }) {
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = useId();
  const confirm = (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim() || busy) return;
    setBusy(true);
    setError(null);
    crewApi
      .confirmNotifyTarget(targetId, code.trim())
      .then(() => {
        toast.success('Email confirmed. Alerts will reach it.');
        onDone();
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setBusy(false));
  };
  return (
    <form onSubmit={confirm} className="flex basis-full flex-wrap items-center gap-2" noValidate>
      <label htmlFor={id} className="text-xs text-ink-2">
        Enter the code we emailed to this address:
      </label>
      <input
        id={id}
        value={code}
        onChange={(e) => setCode(e.target.value)}
        autoComplete="one-time-code"
        spellCheck={false}
        maxLength={32}
        className="rr-input w-32 px-2 py-1 font-mono text-[12px] uppercase"
      />
      <button type="submit" disabled={busy || !code.trim()} className="rr-btn-ghost inline-flex items-center gap-1.5 px-2.5 py-1 text-xs">
        {busy && <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />} Confirm
      </button>
      {error && (
        <span role="alert" className="basis-full text-xs text-fail">
          {error}
        </span>
      )}
    </form>
  );
}

export function NotifyTargets({ highlight = false }: { highlight?: boolean }) {
  const rules = useResource('crew-notify-rules', () => crewApi.notificationRules() as Promise<unknown> as Promise<NotificationRules>);
  const crews = useCrewList();
  const [kind, setKind] = useState<'email' | 'webhook'>('email');
  const [target, setTarget] = useState('');
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [added, setAdded] = useState<AddedTarget | null>(null);
  const [turnedOn, setTurnedOn] = useState<string[]>([]);
  const inputId = useId();
  const hintId = useId();
  const problem = targetProblem(kind, target);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (problem || busy) return;
    setBusy(true);
    setError(null);
    crewApi
      .addNotifyTarget(kind, target.trim())
      .then((out) => {
        setAdded(out as unknown as AddedTarget);
        setTarget('');
        setTouched(false);
        setTurnedOn([]);
        rules.refresh();
        const sent = (out as { confirmation?: string }).confirmation === 'sent';
        toast.success(kind === 'webhook' ? 'Webhook verified and saved.' : sent ? 'Check that inbox for a confirmation code.' : 'Email saved.');
      })
      .catch((err: unknown) => setError(actionError(err)))
      .finally(() => setBusy(false));
  };

  const targets = rules.data?.targets ?? [];
  const realtimeKinds = (rules.data?.defaults ?? []).filter((d) => d.realtime).map((d) => kindTitle(d.kind));
  const missing = (added?.crews_without_channel ?? []).filter((id) => !turnedOn.includes(id));

  return (
    <section
      id="realtime-alerts"
      aria-labelledby={`${inputId}-title`}
      className={clsx('rr-card crew-px rounded-[3px]', highlight && 'outline outline-2 outline-offset-2 outline-signal')}
    >
      <div className="flex flex-wrap items-start gap-3 px-4 pt-4 sm:px-5">
        <span className="mt-1 text-ink">
          <PixelGlyph name="wire" size={20} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="rr-eyebrow">Real-time alerts</p>
          <h2 id={`${inputId}-title`} className="font-display mt-1 text-lg font-bold text-ink">
            Where should we reach you when a baton drops?
          </h2>
          <p className="mt-1 max-w-2xl text-sm text-ink-2">
            Instant, not a digest{realtimeKinds.length ? `: ${realtimeKinds.join(', ').toLowerCase()}` : ''}. Several events within 2 minutes arrive as one
            message; quiet hours follow {rules.data?.quiet_hours_tz ?? 'US Eastern'} time, and a blip shorter than 20 minutes never alerts.
          </p>
        </div>
      </div>

      <div className="px-4 pb-4 pt-3 sm:px-5">
        {rules.data && targets.length > 0 && (
          <ul className="mb-3 divide-y divide-rule border-y border-rule">
            {targets.map((t) => (
              <li key={t.id} className="flex flex-wrap items-center gap-2 py-2">
                <PixelGlyph name={t.kind === 'email' ? 'mail' : 'wire'} size={13} className="text-ink-2" />
                <span className="min-w-0 truncate font-mono text-[12px] text-ink">{t.target}</span>
                <Pill tone={t.verified_at ? 'ok' : 'neutral'}>
                  {t.verified_at ? (t.kind === 'webhook' ? 'verified (HMAC)' : 'confirmed') : 'waiting for the code'}
                </Pill>
                {!t.verified_at && t.kind === 'email' && <ConfirmEmail targetId={t.id} onDone={rules.refresh} />}
                {t.verified_at && (
                  <span className="ml-auto font-mono text-[11px] text-ink-3" title={absoluteTime(t.verified_at)}>
                    since {absoluteTime(t.verified_at)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
        {rules.data && targets.length === 0 && <p className="mb-3 font-mono text-[12px] text-signal-ink">No targets yet: alerts only show in the bell.</p>}
        {rules.error != null && !rules.data && <p className="mb-3 text-sm text-ink-2">Alert settings are not available on this server yet.</p>}

        <form onSubmit={submit} className="space-y-2" noValidate>
          <div role="radiogroup" aria-label="Target kind" className="flex gap-1.5">
            {(['email', 'webhook'] as const).map((k) => (
              <button
                key={k}
                type="button"
                role="radio"
                aria-checked={kind === k}
                onClick={() => {
                  setKind(k);
                  setError(null);
                }}
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-[2px] border px-2.5 py-1 font-mono text-[11px]',
                  kind === k ? 'border-ink bg-ink text-paper' : 'border-rule text-ink-2 hover:border-ink',
                )}
              >
                <PixelGlyph name={k === 'email' ? 'mail' : 'wire'} size={11} mono={kind === k} /> {k === 'email' ? 'Email' : 'Signed webhook'}
              </button>
            ))}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row">
            <label htmlFor={inputId} className="sr-only">
              {kind === 'email' ? 'Email address' : 'Webhook URL (https)'}
            </label>
            <input
              id={inputId}
              value={target}
              onChange={(e) => {
                setTarget(e.target.value);
                setError(null);
              }}
              onBlur={() => setTouched(true)}
              type={kind === 'email' ? 'email' : 'url'}
              inputMode={kind === 'email' ? 'email' : 'url'}
              autoComplete={kind === 'email' ? 'email' : 'off'}
              spellCheck={false}
              placeholder={kind === 'email' ? 'you@example.com' : 'https://bridge.example.com/remembra'}
              aria-invalid={touched && !!problem}
              aria-describedby={hintId}
              className="rr-input min-w-0 flex-1 px-3 py-2 font-mono text-sm"
            />
            <button type="submit" disabled={busy} className="rr-btn-primary inline-flex items-center justify-center gap-2 px-4 py-2 text-sm">
              {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              {kind === 'webhook' ? (busy ? 'Sending the challenge…' : 'Verify and save') : 'Save email'}
            </button>
          </div>
          <p id={hintId} className={clsx('text-xs', touched && problem ? 'text-fail' : 'text-ink-3')}>
            {touched && problem
              ? problem
              : kind === 'webhook'
                ? 'We POST a signed challenge first; your endpoint must echo it back. Only then is it saved.'
                : 'We email a confirmation code first (your own login email needs none). Alerts go out the moment they happen.'}
          </p>
          {error && (
            <p role="alert" className="border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
              {error}
            </p>
          )}
        </form>

        {added?.signing_secret && (
          <div className="mt-4 space-y-2 border-l-[3px] border-signal bg-signal-wash px-3 py-3">
            <p className="text-sm font-semibold text-ink">Signing secret for {added.target}. Shown once: store it in your bridge now.</p>
            <CopyCommand command={added.signing_secret} label="Webhook signing secret" toastText="Secret copied" />
            <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-ink-2">{webhookRecipe(added.signature_header ?? 'X-Remembra-Signature')}</pre>
            <button type="button" onClick={() => setAdded({ ...added, signing_secret: undefined })} className="rr-btn-ghost px-2.5 py-1 text-xs">
              I stored it
            </button>
          </div>
        )}
        {added && missing.length > 0 && (
          <ul className="mt-3 space-y-2">
            {missing.map((id) => {
              const project = crews.items.find((c) => c.crew.id === id)?.crew.project_id ?? id;
              return <TurnOn key={id} crewId={id} project={project} kind={added.kind} onDone={() => setTurnedOn((prev) => [...prev, id])} />;
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
