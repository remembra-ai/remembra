import { useEffect, useState } from 'react';
import { EMPTY_AUTH_CONFIG, fetchAuthConfig, type AuthConfig } from '../lib/authProviders';

export interface AuthConfigState {
  config: AuthConfig;
  loading: boolean;
  /** The config could not be loaded: social buttons stay hidden, signup still posts. */
  failed: boolean;
}

/** GET /api/v1/auth/providers once per mount. */
export function useAuthConfig(): AuthConfigState {
  const [state, setState] = useState<AuthConfigState>({ config: EMPTY_AUTH_CONFIG, loading: true, failed: false });

  useEffect(() => {
    const controller = new AbortController();
    fetchAuthConfig(controller.signal)
      .then((config) => setState({ config, loading: false, failed: false }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        console.warn('Sign-in options unavailable:', error);
        setState({ config: EMPTY_AUTH_CONFIG, loading: false, failed: true });
      });
    return () => controller.abort();
  }, []);

  return state;
}
