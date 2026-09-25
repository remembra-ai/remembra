import { useEffect, useRef, useState } from 'react';
import { loadTurnstile, type TurnstileApi } from '../../lib/turnstile';

interface TurnstileWidgetProps {
  siteKey: string;
  /** A fresh token, or null when the previous one expired / errored / was reset. */
  onToken: (token: string | null) => void;
  /** Increment to get a new challenge (tokens are single use: reset after every submit). */
  resetKey: number;
  dark: boolean;
}

/** Cloudflare Turnstile, explicitly rendered. Only mounted when the server publishes a site key. */
export function TurnstileWidget({ siteKey, onToken, resetKey, dark }: TurnstileWidgetProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const widgetRef = useRef<{ api: TurnstileApi; id: string } | null>(null);
  const onTokenRef = useRef(onToken);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    onTokenRef.current = onToken;
  }, [onToken]);

  useEffect(() => {
    let cancelled = false;
    loadTurnstile()
      .then((api) => {
        if (cancelled || !containerRef.current) return;
        const id = api.render(containerRef.current, {
          sitekey: siteKey,
          action: 'signup',
          theme: dark ? 'dark' : 'light',
          size: 'flexible',
          callback: (token) => onTokenRef.current(token),
          'expired-callback': () => onTokenRef.current(null),
          'error-callback': () => onTokenRef.current(null),
        });
        widgetRef.current = { api, id };
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
      const widget = widgetRef.current;
      widgetRef.current = null;
      if (widget) widget.api.remove(widget.id);
    };
  }, [siteKey, dark]);

  useEffect(() => {
    if (resetKey === 0) return;
    const widget = widgetRef.current;
    onTokenRef.current(null);
    if (widget) widget.api.reset(widget.id);
  }, [resetKey]);

  return (
    <div>
      <div ref={containerRef} className="min-h-[65px]" />
      {loadError && (
        <p className="mt-2 text-sm text-red-400" role="alert">
          The human check could not load. Disable content blockers for challenges.cloudflare.com and reload the page.
        </p>
      )}
    </div>
  );
}
