// Run a human-only crew action; on 401 step_up_required, ask for the password
// (StepUpDialog), store the fresh login and retry once (§5.9).

import { useCallback, useRef, useState } from 'react';
import { api } from '../../../lib/api';
import { API_V1 } from '../../../config';
import { needsStepUp, reauthenticate, signedInUser } from './stepUp';

export interface StepUpPrompt {
  email: string;
  what: string;
  busy: boolean;
  error: string | null;
  submit: (password: string) => void;
  cancel: () => void;
}

export interface HumanActionRunner {
  /** Resolves with the action's result; rejects with its error (or 'cancelled' when the prompt was dismissed). */
  run: <T>(what: string, action: () => Promise<T>) => Promise<T>;
  /** Non-null while the password prompt is open. */
  prompt: StepUpPrompt | null;
}

export class StepUpCancelled extends Error {
  constructor() {
    super('Cancelled.');
    this.name = 'StepUpCancelled';
  }
}

export function useHumanAction(): HumanActionRunner {
  const [prompt, setPrompt] = useState<Omit<StepUpPrompt, 'submit' | 'cancel'> | null>(null);
  const pending = useRef<{ resolve: (password: string) => void; reject: (err: Error) => void } | null>(null);

  const ask = useCallback((email: string, what: string, error: string | null = null) => {
    setPrompt({ email, what, busy: false, error });
    return new Promise<string>((resolve, reject) => {
      pending.current = { resolve, reject };
    });
  }, []);

  const run = useCallback(
    async <T,>(what: string, action: () => Promise<T>): Promise<T> => {
      try {
        return await action();
      } catch (err) {
        if (!needsStepUp(err)) throw err;
        const user = signedInUser();
        if (!user) throw err;
        let error: string | null = null;
        for (;;) {
          const password = await ask(user.email, what, error);
          setPrompt((p) => (p ? { ...p, busy: true, error: null } : p));
          try {
            const token = await reauthenticate((i, init) => fetch(i, init), API_V1, user.email, password);
            api.setJwtToken(token);
            break;
          } catch (e) {
            error = e instanceof Error ? e.message : String(e);
          }
        }
        try {
          return await action();
        } finally {
          setPrompt(null);
        }
      }
    },
    [ask],
  );

  const submit = useCallback((password: string) => {
    pending.current?.resolve(password);
  }, []);
  const cancel = useCallback(() => {
    pending.current?.reject(new StepUpCancelled());
    pending.current = null;
    setPrompt(null);
  }, []);

  return { run, prompt: prompt ? { ...prompt, submit, cancel } : null };
}
