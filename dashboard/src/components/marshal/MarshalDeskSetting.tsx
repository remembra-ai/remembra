// Settings > Diagnostics: the account's switch for the Marshal desk. Shown
// only when the server has a desk for this account (GET /marshal/settings
// answered 200). Turning it off unmounts the desk at once, once the server
// has stored it; the rules-only why? slips keep working either way.

import { useId, useState, type ChangeEvent } from 'react';
import { Loader2 } from 'lucide-react';
import { TrailMark } from '../../brand/TrailMark';
import { useMarshalDesk } from '../../hooks/marshalDesk';
import { DESK_COPY, deskToggle, noticeFor, setDeskSettings } from '../../lib/marshalDesk';

/** The card itself, from its state (the tests render it as it is). */
export function MarshalDeskSettingCard({
  id,
  enabled,
  busy,
  error,
  onChange,
}: {
  id: string;
  enabled: boolean;
  busy: boolean;
  error: string | null;
  onChange: (desk: boolean) => void;
}) {
  const helpId = `${id}-help`;
  return (
    <section aria-labelledby={`${id}-title`} className="rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-start gap-4">
        <div className="rounded-lg bg-gray-100 p-3 text-ink dark:bg-gray-700" aria-hidden="true">
          <TrailMark className="h-6 w-6" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 id={`${id}-title`} className="text-lg font-semibold text-gray-900 dark:text-white">
            <label htmlFor={id} className="flex cursor-pointer items-center gap-3">
              <input
                id={id}
                type="checkbox"
                checked={enabled}
                disabled={busy}
                aria-describedby={helpId}
                onChange={(event: ChangeEvent<HTMLInputElement>) => onChange(event.target.checked)}
                className="h-4 w-4 shrink-0 accent-[var(--signal)]"
              />
              {DESK_COPY.settingLabel}
              {busy && <Loader2 className="h-4 w-4 animate-spin text-ink-3" aria-hidden="true" />}
            </label>
          </h3>
          <p id={helpId} className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {DESK_COPY.settingHelp}
          </p>
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
            Uses the operator's model, separately from enrichment settings. Questions and permitted record excerpts
            reach that provider. Model usage is separate from smart credits.
          </p>
          <a href="https://docs.remembra.dev/guides/marshal-desk/" target="_blank" rel="noopener noreferrer"
            className="mt-2 inline-block text-sm underline">
            How the desk works and its limits
          </a>
          {error && (
            <p role="alert" className="mt-3 text-sm text-fail">
              {error}
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

export function MarshalDeskSetting() {
  const desk = useMarshalDesk();
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!desk.available) return null;
  const toggle = deskToggle(setDeskSettings, desk.setDesk);
  return (
    <MarshalDeskSettingCard
      id={id}
      enabled={!desk.optedOut}
      busy={busy}
      error={error}
      onChange={(next) => {
        setBusy(true);
        setError(null);
        toggle(next)
          .catch((err: unknown) => setError(noticeFor(err).message))
          .finally(() => setBusy(false));
      }}
    />
  );
}
