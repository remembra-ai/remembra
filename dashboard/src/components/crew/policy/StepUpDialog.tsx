// "Confirm it's you": the step-up prompt for human-only actions that need a
// login from the last 15 minutes (§5.9). Same account, password only.

import { useId, useState } from 'react';
import { KeyRound, Loader2 } from 'lucide-react';
import { Modal } from '../zones/Modal';
import type { StepUpPrompt } from './useHumanAction';

export function StepUpDialog({ prompt }: { prompt: StepUpPrompt }) {
  const id = useId();
  const [password, setPassword] = useState('');
  return (
    <Modal labelledBy={`${id}-t`} onClose={prompt.cancel} className="cz-dialog p-5" top initialFocus="input">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (password && !prompt.busy) prompt.submit(password);
        }}
      >
        <p className="rr-eyebrow">Confirm it's you</p>
        <h2 id={`${id}-t`} className="font-display mt-1 flex items-center gap-2 text-xl font-bold text-ink">
          <KeyRound className="h-5 w-5 text-signal" aria-hidden="true" /> Sign in again
        </h2>
        <p className="mt-2 text-sm text-ink-2">
          {prompt.what} needs a login from the last 15 minutes. Enter the password for <span className="font-mono text-ink">{prompt.email}</span>.
        </p>
        <label className="mt-4 block text-sm">
          <span className="font-mono text-[11px] uppercase tracking-[0.08em] text-ink-3">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            className="rr-input mt-1 w-full px-2 py-2 text-[14px]"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {prompt.error && (
          <p role="alert" className="mt-3 border-l-[3px] border-fail bg-fail-wash px-3 py-2 text-sm text-ink">
            {prompt.error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" className="rr-btn-ghost px-3 py-2 text-sm" onClick={prompt.cancel}>
            Cancel
          </button>
          <button type="submit" disabled={!password || prompt.busy} className="rr-btn-primary inline-flex items-center gap-2 px-3 py-2 text-sm">
            {prompt.busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
            Confirm
          </button>
        </div>
      </form>
    </Modal>
  );
}
