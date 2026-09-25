// Settings -> Apps & connections: the apps connected to your memory through
// the remote MCP connector (Claude, ChatGPT), with what each may do, which
// projects it sees, when it was last used, and a revoke button.
// Data: GET/DELETE /api/v1/connector/connections (dashboard sign-in only).

import { useId, useState } from 'react';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { api, ApiError, type ConnectorConnection } from '../lib/api';
import { relativeTime } from '../lib/time';
import { useNow, useResource } from '../hooks/useResource';
import { Card, CardHeader, ErrorNotice, Pill, Skeleton } from './relay/ui';

function monogram(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)).toUpperCase();
}

function ConnectionRow({
  connection,
  scopeText,
  now,
  onRevoked,
}: {
  connection: ConnectorConnection;
  scopeText: Record<string, string>;
  now: Date;
  onRevoked: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = connection.client_name || connection.client_id;

  const revoke = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.revokeConnection(connection.connection_id);
      onRevoked();
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) onRevoked();
      else setError(err instanceof Error ? err.message : 'Could not revoke this app.');
      setBusy(false);
    }
  };

  return (
    <li className="grid gap-3 py-4 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-start">
      <span aria-hidden="true" className="flex h-10 w-10 items-center justify-center bg-ink font-mono text-xs font-bold text-paper">
        {monogram(name)}
      </span>
      <div className="min-w-0">
        <p className="flex flex-wrap items-center gap-2">
          <span className="font-display text-lg font-bold text-ink">{name}</span>
          <Pill>writes as {connection.agent_id}</Pill>
        </p>
        <ul className="mt-1.5 space-y-0.5 text-sm text-ink-2">
          {connection.scopes.map((scope) => (
            <li key={scope} className="flex gap-2">
              <span aria-hidden="true" className="mt-[7px] h-1.5 w-1.5 shrink-0 bg-signal" />
              <span>
                {scopeText[scope] ?? scope} <span className="font-mono text-[11px] text-ink-3">{scope}</span>
              </span>
            </li>
          ))}
        </ul>
        <p className="mt-2 font-mono text-[11px] text-ink-3">
          {connection.project_ids.length ? `projects: ${connection.project_ids.join(', ')}` : 'all projects'} · connected{' '}
          {connection.created_at ? relativeTime(connection.created_at, now) : 'unknown'} ·{' '}
          {connection.last_used_at ? `last used ${relativeTime(connection.last_used_at, now)}` : 'not used yet'}
        </p>
        {error && (
          <p role="alert" className="mt-2 text-sm text-fail">
            {error}
          </p>
        )}
      </div>
      <div className="flex items-center gap-2 sm:justify-end">
        {confirming ? (
          <>
            <button type="button" onClick={() => setConfirming(false)} disabled={busy} className="rr-btn-ghost px-3 py-1.5 text-sm">
              Keep
            </button>
            <button
              type="button"
              onClick={revoke}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-[3px] border border-fail bg-fail px-3 py-1.5 text-sm font-semibold text-paper disabled:opacity-60"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
              Revoke {name}
            </button>
          </>
        ) : (
          <button type="button" onClick={() => setConfirming(true)} className="rr-btn-ghost px-3 py-1.5 text-sm hover:!border-fail hover:!text-fail">
            Revoke
          </button>
        )}
      </div>
      {confirming && !busy && (
        <p className="text-xs text-ink-3 sm:col-span-3 sm:col-start-2">
          {name} loses access right away: its tokens stop working and it has to be connected again to reach your memory.
        </p>
      )}
    </li>
  );
}

export function Connections() {
  const titleId = useId();
  const jwt = api.getAuthMode() === 'jwt';
  const connections = useResource(jwt ? 'connector-connections' : null, () => api.listConnections());
  const now = useNow(60000);
  const status = connections.error instanceof ApiError ? connections.error.status : null;

  return (
    <div className="space-y-5">
      <Card labelledBy={titleId}>
        <CardHeader
          id={titleId}
          eyebrow="Remote connector"
          title="Apps & connections"
          action={connections.data ? <Pill>{connections.data.count} connected</Pill> : undefined}
        />
        <div className="px-4 pb-4 pt-2 sm:px-5">
          <p className="max-w-2xl text-sm text-ink-2">
            Apps you connected to your memory through the Remembra connector, such as Claude or ChatGPT. Each keeps the access you
            approved until you revoke it here. Agents on your machines use API keys instead; those live under API keys.
          </p>

          {!jwt && (
            <p className="mt-4 border-l-[3px] border-rule-strong px-3 py-2 text-sm text-ink-2">
              Connected apps are tied to your account, so this list needs an email sign-in. You are signed in with an API key: sign out
              and sign in with your email to see and revoke them.
            </p>
          )}

          {jwt && connections.loading && (
            <div className="mt-4 space-y-3" role="status" aria-label="Loading connections">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          )}

          {jwt && status === 404 && (
            <p className="mt-4 border-l-[3px] border-rule-strong px-3 py-2 text-sm text-ink-2">
              This server does not run the remote connector, so no apps can connect to it. Self-hosting? Set{' '}
              <code className="font-mono text-[13px]">REMEMBRA_CONNECTOR_ENABLED=true</code> and{' '}
              <code className="font-mono text-[13px]">REMEMBRA_PUBLIC_URL</code> to turn it on.
            </p>
          )}

          {jwt && connections.error != null && status !== 404 && !connections.data && (
            <div className="mt-3">
              <ErrorNotice compact error={connections.error} what="your connected apps" onRetry={connections.refresh} />
            </div>
          )}

          {connections.data && connections.data.connections.length === 0 && (
            <div className="mt-4 border border-dashed border-rule px-4 py-5 text-sm text-ink-2">
              <p className="font-semibold text-ink">No apps connected.</p>
              <p className="mt-1">
                In Claude or ChatGPT, add a custom connector with your server's MCP address:{' '}
                <code className="font-mono text-[13px] [overflow-wrap:anywhere]">{api.getApiBaseUrl()}/mcp</code>. You approve it once and it
                shows up here.
              </p>
            </div>
          )}

          {connections.data && connections.data.connections.length > 0 && (
            <ul className={clsx('mt-2 divide-y divide-rule border-y border-rule')}>
              {connections.data.connections.map((connection) => (
                <ConnectionRow
                  key={connection.connection_id}
                  connection={connection}
                  scopeText={connections.data!.scopes}
                  now={now}
                  onRevoked={connections.refresh}
                />
              ))}
            </ul>
          )}
        </div>
      </Card>
    </div>
  );
}
